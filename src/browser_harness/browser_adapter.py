"""A narrow, testable adapter around :mod:`browser_harness.helpers`.

The upstream harness owns CDP, daemon and tab lifecycle.  TaskLens should not
duplicate those concerns in its agent loop, so this module translates a small
structured action protocol into helper calls and translates helper failures
into stable, serialisable outcomes.  The adapter is deliberately dependency
injected: unit tests can provide a fake helper object and never need to start
Chrome or the daemon.
"""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlsplit, urlunsplit

from pydantic import model_validator

from . import helpers as default_helpers
from .tasklens_domain import Action as DomainAction
from .tasklens_domain import ActionOutcome as DomainActionOutcome
from .tasklens_domain import Observation as DomainObservation


class FailureKind(StrEnum):
    """Failure categories exposed by the adapter without leaking CDP errors."""

    NONE = "none"
    ENVIRONMENT = "environment"
    CONNECTION = "connection"
    ACTION = "action"
    ASSERTION = "assertion"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MODEL = "model"
    UNKNOWN = "unknown"


_FAILURE_VALUES = {item.value for item in FailureKind}
_ACTION_ALIASES = {
    "goto": "goto",
    "go_to": "goto",
    "navigate": "goto",
    "navigation": "goto",
    "open": "goto",
    "url": "goto",
    "click": "click",
    "press": "click",
    "tap": "click",
    "type": "type",
    "fill": "type",
    "fill_input": "type",
    "input": "type",
    "write": "type",
    "scroll": "scroll",
    "wait": "wait",
    "sleep": "wait",
    "wait_for_element": "wait",
    "wait_for_load": "wait",
    "wait_for_network_idle": "wait",
    "key": "key",
    "press_key": "key",
    "screenshot": "screenshot",
    "capture_screenshot": "screenshot",
    "finish": "finish",
    "done": "finish",
    "complete": "finish",
    "stop": "finish",
}
_SAFE_ACTIONS = {"goto", "scroll", "wait", "screenshot", "finish"}
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(token|password|passwd|secret|cookie|authorization|api[_-]?key|session|credential)"
    r"\s*([:=])\s*([^,;\s]+)"
)


class BrowserAdapterError(RuntimeError):
    """An adapter boundary error with an already classified failure kind."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str = FailureKind.UNKNOWN.value,
        retryable: bool = False,
    ) -> None:
        self.failure_kind = _normalise_failure_kind(failure_kind)
        self.retryable = retryable
        # Runtime implementations may only see ``str(exc)`` rather than the
        # structured attribute.  Include the stable category in the bounded
        # message so the classification survives that compatibility boundary.
        text = str(message)
        if self.failure_kind not in text.lower():
            text = f"{self.failure_kind}: {text}"
        super().__init__(text)


class InvalidActionError(ValueError):
    """Raised when model output cannot be converted to a safe Action."""

    failure_kind = FailureKind.MODEL.value
    retryable = False


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A pointer to evidence, never the binary evidence payload itself."""

    kind: str
    location: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return self.location

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "location": self.location, "metadata": dict(self.metadata)}


class Action(DomainAction):
    """Domain ``Action`` with compatibility aliases used by the adapter.

    The canonical Pydantic contract lives in ``tasklens_domain``.  Keeping
    this subclass here lets older/fake model providers use ``name`` and
    ``params`` while Runtime still receives a domain-compatible model.
    """

    @property
    def name(self) -> str:
        return self.kind

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.args)

    @model_validator(mode="before")
    @classmethod
    def _accept_adapter_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "params" in data and "args" not in data:
            data["args"] = data.pop("params")
        if "selector" in data and "target" not in data:
            data["target"] = data.pop("selector")
        if "text" in data and "value" not in data:
            data["value"] = data.pop("text")
        raw_kind = data.get("kind") or data.get("name") or data.get("action") or data.get("type")
        if raw_kind is not None:
            data["kind"] = _normalise_kind(raw_kind)
        args = data.get("args")
        if isinstance(args, Mapping):
            copied_args = dict(args)
            if data.get("kind") == "goto" and not data.get("url") and copied_args.get("url"):
                data["url"] = copied_args.pop("url")
            if data.get("kind") == "wait" and not data.get("condition") and copied_args.get("condition"):
                data["condition"] = copied_args.pop("condition")
            data["args"] = copied_args
        return data


class Observation(DomainObservation):
    """Domain observation with reader-friendly adapter aliases."""

    @property
    def page_summary(self) -> str:
        return self.text

    @property
    def interactive_elements(self) -> list[dict[str, Any]]:
        raw = self.metadata.get("interactive_elements", [])
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
            return [dict(item) for item in raw if isinstance(item, Mapping)]
        return [
            {"selector": key, **element.model_dump()}
            for key, element in self.elements.items()
        ]

    @property
    def viewport(self) -> dict[str, int]:
        value = self.metadata.get("viewport", {})
        return dict(value) if isinstance(value, Mapping) else {}

    @property
    def raw_page_info(self) -> dict[str, Any]:
        value = self.metadata.get("raw_page_info", {})
        return dict(value) if isinstance(value, Mapping) else {}

    @model_validator(mode="before")
    @classmethod
    def _accept_adapter_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        metadata = dict(data.get("metadata") or {}) if isinstance(data.get("metadata"), Mapping) else {}
        if "page_summary" in data and "text" not in data:
            data["text"] = data.pop("page_summary")
        if "interactive_elements" in data and "elements" not in data:
            interactive = data.pop("interactive_elements")
            metadata["interactive_elements"] = list(interactive) if isinstance(interactive, Sequence) and not isinstance(interactive, (str, bytes, bytearray)) else []
            data["elements"] = metadata["interactive_elements"]
        if "viewport" in data:
            metadata["viewport"] = dict(data.pop("viewport") or {})
        if "raw_page_info" in data:
            metadata["raw_page_info"] = dict(data.pop("raw_page_info") or {})
        data["metadata"] = metadata
        return data


class ActionOutcome(DomainActionOutcome):
    """Domain outcome with status/value/error aliases for Runtime callers."""

    status: str | None = None
    value: Any = None

    @model_validator(mode="before")
    @classmethod
    def _accept_adapter_aliases(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "error_summary" in data and "error" not in data:
            data["error"] = data.pop("error_summary")
        if "evidence_ref" in data and "evidence" not in data:
            evidence = data.pop("evidence_ref")
            if isinstance(evidence, EvidenceRef):
                evidence = evidence.as_dict()
            data["evidence"] = evidence
        return data

    def model_post_init(self, __context: Any, /) -> None:  # pragma: no cover - pydantic hook
        if self.status is None:
            object.__setattr__(self, "status", "success" if self.success else "failed")
        if self.success and self.failure_kind != FailureKind.NONE.value:
            object.__setattr__(self, "failure_kind", FailureKind.NONE.value)
        super().model_post_init(__context)

    @property
    def error_summary(self) -> str | None:
        return self.error

    @property
    def evidence_ref(self) -> EvidenceRef | None:
        value = self.evidence
        if isinstance(value, Mapping) and value.get("location"):
            return EvidenceRef(
                kind=str(value.get("kind", "unknown")),
                location=str(value["location"]),
                metadata=value.get("metadata", {}),
            )
        return None


@runtime_checkable
class BrowserAdapter(Protocol):
    """Protocol consumed by the single-agent Runtime."""

    async def attach(self) -> None: ...

    async def observe(self) -> Observation: ...

    async def execute(self, action: Action | Mapping[str, Any] | Any) -> ActionOutcome: ...

    async def capture_evidence(self) -> EvidenceRef: ...

    async def close(self) -> None: ...


def _normalise_failure_kind(value: Any, default: str = FailureKind.UNKNOWN.value) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "ok": "none",
        "success": "none",
        "succeeded": "none",
        "env": "environment",
        "browser": "environment",
        "cdp": "connection",
        "websocket": "connection",
        "exec": "action",
        "verification": "assertion",
        "llm": "model",
    }
    text = aliases.get(text, text)
    return text if text in _FAILURE_VALUES else default


def classify_failure(error: BaseException) -> str:
    """Map helper/daemon exceptions to the stable adapter taxonomy."""

    explicit = getattr(error, "failure_kind", None)
    if explicit:
        return _normalise_failure_kind(explicit)
    if isinstance(error, (TimeoutError,)):
        return FailureKind.TIMEOUT.value
    if isinstance(error, (ConnectionError, BrokenPipeError)):
        return FailureKind.CONNECTION.value
    if isinstance(error, (FileNotFoundError, PermissionError)):
        return FailureKind.ENVIRONMENT.value

    message = str(error).lower()
    if any(token in message for token in ("timed out", "timeout", "deadline exceeded")):
        return FailureKind.TIMEOUT.value
    if any(
        token in message
        for token in (
            "websocket",
            "connection refused",
            "connection reset",
            "socket",
            "daemon",
            "no session",
            "session not found",
            "target closed",
            "target not found",
            "browser disconnected",
        )
    ):
        return FailureKind.CONNECTION.value
    if any(
        token in message
        for token in (
            "chrome executable",
            "chromium executable",
            "browser executable",
            "no such file",
            "display not found",
            "failed to launch browser",
        )
    ):
        return FailureKind.ENVIRONMENT.value
    if any(token in message for token in ("assertion", "expected", "business result")):
        return FailureKind.ASSERTION.value
    if any(token in message for token in ("element", "selector", "click", "input", "invalid parameter")):
        return FailureKind.ACTION.value
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return FailureKind.ACTION.value
    return FailureKind.UNKNOWN.value


def _normalise_kind(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    return _ACTION_ALIASES.get(raw, raw)


def _mapping_value(data: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def normalize_action(raw: Action | DomainAction | Mapping[str, Any] | Any) -> Action:
    """Convert model output or a fake action object to a canonical Action.

    This function copies all mappings before reading them and never evaluates
    arbitrary Python supplied by the model.  Unknown action kinds are retained
    for the execute boundary to classify as ``model`` failures.
    """

    if isinstance(raw, Action):
        return raw
    if isinstance(raw, DomainAction):
        data = raw.model_dump()
    elif isinstance(raw, Mapping):
        data = dict(raw)
    else:
        if raw is None or isinstance(raw, (str, bytes, bytearray, int, float, bool)):
            raise InvalidActionError("action must be a structured mapping or Action object")
        data = {}
        for key in ("kind", "name", "action", "type", "target", "selector", "element", "value", "text", "url", "condition", "args", "params", "arguments", "idempotent", "metadata"):
            if hasattr(raw, key):
                data[key] = getattr(raw, key)

    kind = _normalise_kind(_mapping_value(data, "kind", "name", "action", "type"))
    if not kind:
        raise InvalidActionError("action kind is required")
    args_raw = _mapping_value(data, "args", "params", "arguments")
    if args_raw is None:
        args: dict[str, Any] = {}
    elif isinstance(args_raw, Mapping):
        args = dict(args_raw)
    else:
        raise InvalidActionError("action args must be a mapping")

    target = _mapping_value(data, "target", "selector", "element", "locator")
    value = _mapping_value(data, "value", "text", "reason")
    url = _mapping_value(data, "url") or args.get("url")
    condition = _mapping_value(data, "condition") or args.get("condition")
    # ``params``/``args`` often carries the same top-level fields.  Promote
    # those protocol fields once and remove them from the free-form argument
    # map so equality, serialization and helper dispatch stay deterministic.
    args.pop("url", None)
    args.pop("condition", None)

    if isinstance(target, Mapping):
        target_map = dict(target)
        if "x" in target_map and "y" in target_map:
            args.setdefault("x", target_map["x"])
            args.setdefault("y", target_map["y"])
            target = None
        else:
            target = target_map.get("selector") or target_map.get("css") or target_map.get("name")
    if target is not None and not isinstance(target, str):
        target = str(target)

    if kind == "goto" and not url:
        raise InvalidActionError("goto action requires url")
    if kind == "type" and (target is None or value is None):
        raise InvalidActionError("type action requires target and value")
    if kind == "click" and target is None and not {"x", "y"} <= set(args):
        raise InvalidActionError("click action requires target or x/y coordinates")

    idempotent = _mapping_value(data, "idempotent", "is_idempotent")
    if idempotent is None:
        idempotent = kind in _SAFE_ACTIONS
    metadata = _mapping_value(data, "metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise InvalidActionError("action metadata must be a mapping")

    return Action(
        kind=kind,
        target=target,
        value=value,
        url=str(url) if url is not None else None,
        condition=str(condition) if condition is not None else None,
        args=args,
        idempotent=bool(idempotent),
        metadata=dict(metadata),
    )


def _redact_url(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.netloc:
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except ValueError:  # defensive for malformed URLs
        pass
    return text.split("#", 1)[0].split("?", 1)[0]


def _redact_text(value: Any, limit: int = 1_000) -> str:
    text = str(value or "")
    text = _SENSITIVE_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return text[:limit]


def _bounded_mapping(value: Any, limit: int = 100) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in list(value.items())[:limit]}


class CDPBrowserAdapter:
    """Concrete adapter over an injected helpers module/object.

    ``helpers`` defaults to the upstream module for production.  Tests and the
    Runtime can pass a fake object implementing only the operations they need.
    ``clock`` is injectable so duration and timeout behavior are deterministic.
    """

    def __init__(
        self,
        *,
        helpers: Any = default_helpers,
        clock: Callable[[], float] = time.monotonic,
        max_summary_chars: int = 4_000,
        max_interactive_elements: int = 100,
        screenshot_max_dim: int = 1_800,
    ) -> None:
        self.helpers = helpers
        self.clock = clock
        # Keep the lower bound small so callers can use a tiny bound in unit
        # tests and in constrained model contexts.  Production callers still
        # receive the 4k default from the constructor.
        self.max_summary_chars = max(1, int(max_summary_chars))
        self.max_interactive_elements = max(1, int(max_interactive_elements))
        self.screenshot_max_dim = max(1, int(screenshot_max_dim))
        self.attached = False
        self.tab_id: str | None = None
        self._last_observation: Observation | None = None

    async def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        function = getattr(self.helpers, name, None)
        if function is None:
            raise BrowserAdapterError(
                f"browser helper unavailable: {name}",
                failure_kind=FailureKind.ENVIRONMENT.value,
            )
        try:
            result = function(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except BrowserAdapterError:
            raise
        except Exception as exc:
            kind = classify_failure(exc)
            raise BrowserAdapterError(
                _redact_text(exc),
                failure_kind=kind,
                retryable=kind in {FailureKind.CONNECTION.value, FailureKind.TIMEOUT.value},
            ) from exc

    async def attach(self) -> None:
        if self.attached:
            return
        function = getattr(self.helpers, "ensure_real_tab", None)
        if function is not None:
            try:
                tab = function()
                if inspect.isawaitable(tab):
                    tab = await tab
            except Exception as exc:
                kind = classify_failure(exc)
                raise BrowserAdapterError(_redact_text(exc), failure_kind=kind) from exc
            if tab is None:
                raise BrowserAdapterError(
                    "no usable browser tab is available",
                    failure_kind=FailureKind.ENVIRONMENT.value,
                )
            if isinstance(tab, Mapping):
                self.tab_id = str(tab.get("targetId") or tab.get("target_id") or "") or None
            else:
                self.tab_id = str(tab)
        else:
            # A minimal fake may not model tab discovery.  It is still a valid
            # injected adapter and can be used for Runtime tests.
            self.tab_id = None
        self.attached = True
        return

    async def _ensure_attached(self) -> None:
        if not self.attached:
            await self.attach()

    async def observe(self) -> Observation:
        await self._ensure_attached()
        started = self.clock()
        try:
            page_info = await self._call("page_info")
            if not isinstance(page_info, Mapping):
                page_info = {}
            tab_info: Mapping[str, Any] = {}
            current_tab = getattr(self.helpers, "current_tab", None)
            if current_tab is not None:
                tab_info_value = current_tab()
                if inspect.isawaitable(tab_info_value):
                    tab_info_value = await tab_info_value
                if isinstance(tab_info_value, Mapping):
                    tab_info = tab_info_value
            state = await self._extract_page_state()
            summary = _redact_text(
                state.get("summary") or state.get("text") or state.get("page_summary") or "",
                self.max_summary_chars,
            )
            interactive = state.get("interactive_elements") or state.get("elements") or []
            if isinstance(interactive, Mapping):
                interactive = list(interactive.values())
            bounded_interactive = [
                _bounded_mapping(item, 30)
                for item in list(interactive)[: self.max_interactive_elements]
                if isinstance(item, Mapping)
            ]
            url = _redact_url(page_info.get("url") or tab_info.get("url") or "")
            title = _redact_text(page_info.get("title") or tab_info.get("title") or "", 500)
            width = page_info.get("w", page_info.get("width"))
            height = page_info.get("h", page_info.get("height"))
            viewport = {
                key: int(value)
                for key, value in (("width", width), ("height", height))
                if isinstance(value, (int, float))
            }
            elements: dict[str, Any] = {}
            for index, item in enumerate(bounded_interactive):
                selector = str(item.get("selector") or item.get("id") or item.get("name") or index)
                elements[selector] = item
            safe_page_info = dict(page_info)
            if "url" in safe_page_info:
                safe_page_info["url"] = url
            metadata = {
                "interactive_elements": bounded_interactive,
                "viewport": viewport,
                "raw_page_info": safe_page_info,
                "duration_seconds": max(0.0, self.clock() - started),
            }
            observation = Observation(
                url=url,
                title=title,
                text=summary,
                elements=elements,
                structured=_bounded_mapping(state.get("structured", {}), 100),
                tab_id=self.tab_id or str(tab_info.get("targetId") or tab_info.get("target_id") or "") or None,
                metadata=metadata,
            )
            self._last_observation = observation
            return observation
        except BrowserAdapterError:
            raise
        except Exception as exc:
            kind = classify_failure(exc)
            raise BrowserAdapterError(_redact_text(exc), failure_kind=kind) from exc

    async def _extract_page_state(self) -> dict[str, Any]:
        for name in ("observe_page", "extract_page_state", "page_state"):
            function = getattr(self.helpers, name, None)
            if function is not None:
                value = function()
                if inspect.isawaitable(value):
                    value = await value
                return dict(value) if isinstance(value, Mapping) else {}
        function = getattr(self.helpers, "js", None)
        if function is None:
            return {}
        expression = """
(() => {
  const visible = [...document.querySelectorAll('body *')]
    .filter((e) => e.children.length === 0 && (e.innerText || '').trim())
    .slice(0, 100).map((e) => e.innerText.trim()).join(' ');
  const elements = [...document.querySelectorAll('a,button,input,textarea,select,[role]')]
    .slice(0, 100).map((e) => ({
      tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
      name: (e.innerText || e.getAttribute('aria-label') || e.getAttribute('name') || '').trim(),
      selector: e.id ? `#${e.id}` : null,
      disabled: !!e.disabled, checked: !!e.checked
    }));
  return {summary: visible, interactive_elements: elements};
})()
"""
        value = function(expression)
        if inspect.isawaitable(value):
            value = await value
        return dict(value) if isinstance(value, Mapping) else {}

    async def execute(self, raw_action: Action | Mapping[str, Any] | Any) -> ActionOutcome:
        started = self.clock()
        try:
            action = normalize_action(raw_action)
        except Exception as exc:  # noqa: BLE001 - malformed model output is data
            return ActionOutcome(
                success=False,
                status="failed",
                failure_kind=FailureKind.MODEL.value,
                error=_redact_text(exc),
                duration_seconds=max(0.0, self.clock() - started),
                retryable=False,
            )

        try:
            if action.kind in {"goto", "click", "type", "scroll", "wait", "key", "screenshot"}:
                await self._ensure_attached()
            value = await self._dispatch(action)
            duration = max(0.0, self.clock() - started)
            evidence: dict[str, Any] = {}
            if isinstance(value, EvidenceRef):
                evidence = value.as_dict()
                value = None
            return ActionOutcome(
                success=True,
                status="success",
                failure_kind=FailureKind.NONE.value,
                duration_seconds=duration,
                evidence=evidence,
                retryable=False,
                value=value,
            )
        except BrowserAdapterError as exc:
            kind = exc.failure_kind
            return ActionOutcome(
                success=False,
                status="failed",
                failure_kind=kind,
                error=_redact_text(exc),
                duration_seconds=max(0.0, self.clock() - started),
                retryable=bool(action.idempotent and (exc.retryable or kind in {"connection", "timeout"})),
            )
        except Exception as exc:  # noqa: BLE001 - last-resort stable result
            kind = classify_failure(exc)
            return ActionOutcome(
                success=False,
                status="failed",
                failure_kind=kind,
                error=_redact_text(exc),
                duration_seconds=max(0.0, self.clock() - started),
                retryable=bool(action.idempotent and kind in {"connection", "timeout", "action"}),
            )

    async def execute_action(self, action: Action | Mapping[str, Any] | Any) -> ActionOutcome:
        return await self.execute(action)

    async def act(self, action: Action | Mapping[str, Any] | Any) -> ActionOutcome:
        return await self.execute(action)

    async def _dispatch(self, action: Action) -> Any:
        kind = action.kind
        args = dict(action.args)
        if kind == "goto":
            return await self._call("goto_url", action.url)
        if kind == "click":
            return await self._click(action)
        if kind == "type":
            return await self._type(action)
        if kind == "scroll":
            return await self._scroll(action)
        if kind == "wait":
            return await self._wait(action)
        if kind == "key":
            key = action.value or args.get("key") or action.target
            if not key:
                raise InvalidActionError("key action requires key")
            return await self._call("press_key", str(key), modifiers=int(args.get("modifiers", 0)))
        if kind == "screenshot":
            return await self.capture_evidence()
        if kind == "finish":
            return {"reason": str(action.value or args.get("reason") or "finished")}
        raise BrowserAdapterError(
            f"unsupported model action: {kind}",
            failure_kind=FailureKind.MODEL.value,
        )

    async def _click(self, action: Action) -> Any:
        args = dict(action.args)
        if "x" in args and "y" in args:
            x, y = float(args["x"]), float(args["y"])
        elif action.target:
            selector = action.target
            center = await self._call("js", self._center_script(selector))
            if not isinstance(center, Mapping) or not {"x", "y"} <= set(center):
                raise BrowserAdapterError(
                    f"element not found: {selector!r}",
                    failure_kind=FailureKind.ACTION.value,
                )
            x, y = float(center["x"]), float(center["y"])
        else:
            raise InvalidActionError("click action requires target or x/y coordinates")
        return await self._call(
            "click_at_xy",
            x,
            y,
            button=str(args.get("button", "left")),
            clicks=int(args.get("clicks", 1)),
        )

    @staticmethod
    def _center_script(selector: str) -> str:
        # JSON string encoding is also valid JavaScript string encoding and
        # prevents quotes/newlines in a selector from changing the expression.
        encoded = json.dumps(str(selector), ensure_ascii=False)
        return (
            "(() => { const e = document.querySelector("
            + encoded
            + "); if (!e) return null; const r=e.getBoundingClientRect(); "
            "return {x:r.left+r.width/2,y:r.top+r.height/2}; })() /* TASKLENS_ELEMENT_CENTER */"
        )

    async def _type(self, action: Action) -> Any:
        if not action.target:
            raise InvalidActionError("type action requires target")
        args = dict(action.args)
        text = "" if action.value is None else str(action.value)
        if getattr(self.helpers, "fill_input", None) is not None:
            return await self._call(
                "fill_input",
                action.target,
                text,
                clear_first=bool(args.get("clear_first", True)),
                timeout=float(args.get("timeout", 0.0)),
            )
        # A deliberately small fallback for a fake/minimal helper.  It does
        # not execute arbitrary model code; it only uses the existing helper
        # primitives to focus a selector and insert text.
        selector_literal = json.dumps(action.target, ensure_ascii=False)
        await self._call("js", f"document.querySelector({selector_literal})?.focus()")
        return await self._call("type_text", text)

    async def _scroll(self, action: Action) -> Any:
        args = dict(action.args)
        direction = str(args.get("direction") or action.target or "down").lower()
        amount = float(args.get("amount", args.get("distance", 300)))
        signed = -abs(amount) if direction in {"up", "left"} else abs(amount)
        dx = signed if direction in {"left", "right"} else float(args.get("dx", 0.0))
        dy = signed if direction in {"up", "down"} else float(args.get("dy", 0.0))
        viewport = self._last_observation.viewport if self._last_observation else {}
        width, height = float(viewport.get("width", 1_200)), float(viewport.get("height", 800))
        x = float(args.get("x", width / 2))
        y = float(args.get("y", height / 2))
        return await self._call("scroll", x, y, dy=dy, dx=dx)

    async def _wait(self, action: Action) -> Any:
        args = dict(action.args)
        timeout = float(args.get("timeout", 10.0))
        condition = str(action.condition or args.get("condition") or "").lower()
        target = action.target or args.get("selector")
        if target and getattr(self.helpers, "wait_for_element", None) is not None:
            found = await self._call("wait_for_element", str(target), timeout=timeout, visible=bool(args.get("visible", False)))
            if not found:
                raise BrowserAdapterError(
                    f"wait condition timed out: {target!r}",
                    failure_kind=FailureKind.TIMEOUT.value,
                    retryable=True,
                )
            return found
        if condition in {"load", "page_load", "ready"} and getattr(self.helpers, "wait_for_load", None) is not None:
            found = await self._call("wait_for_load", timeout=timeout)
            if not found:
                raise BrowserAdapterError("page load wait timed out", failure_kind=FailureKind.TIMEOUT.value, retryable=True)
            return found
        if condition in {"network_idle", "networkidle"} and getattr(self.helpers, "wait_for_network_idle", None) is not None:
            found = await self._call("wait_for_network_idle", timeout=timeout, idle_ms=int(args.get("idle_ms", 500)))
            if not found:
                raise BrowserAdapterError("network idle wait timed out", failure_kind=FailureKind.TIMEOUT.value, retryable=True)
            return found
        seconds = float(args.get("seconds", args.get("duration", action.value if isinstance(action.value, (int, float)) else 0.0)))
        if seconds > 0:
            return await self._call("wait", seconds)
        raise InvalidActionError("wait action requires a selector, condition, or positive duration")

    async def capture_evidence(self) -> EvidenceRef:
        # Do not implicitly discover or activate a tab here.  Runtime normally
        # calls ``attach`` before evidence capture; keeping this method lazy
        # makes fake adapters and direct unit tests independent of a browser.
        try:
            path = await self._call("capture_screenshot", full=False, max_dim=self.screenshot_max_dim)
            return EvidenceRef(kind="screenshot", location=str(path), metadata={})
        except BrowserAdapterError:
            raise
        except Exception as exc:
            kind = classify_failure(exc)
            raise BrowserAdapterError(_redact_text(exc), failure_kind=kind) from exc

    async def close(self) -> None:
        # The adapter owns a session reference, not the user's browser tab.
        # Deliberately avoid close_tab() so shutting down Runtime cannot close
        # a tab the user opened outside TaskLens.
        self.attached = False
        self.tab_id = None
        self._last_observation = None


HarnessBrowserAdapter = CDPBrowserAdapter
TaskLensBrowserAdapter = CDPBrowserAdapter


__all__ = [
    "Action",
    "ActionOutcome",
    "BrowserAdapter",
    "BrowserAdapterError",
    "CDPBrowserAdapter",
    "EvidenceRef",
    "FailureKind",
    "HarnessBrowserAdapter",
    "InvalidActionError",
    "Observation",
    "TaskLensBrowserAdapter",
    "classify_failure",
    "normalize_action",
]
