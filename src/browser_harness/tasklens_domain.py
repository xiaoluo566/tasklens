"""Domain contracts for the TaskLens single-agent execution loop.

The module deliberately contains no CDP or persistence code.  It defines the
small, serialisable objects shared by the model provider, browser adapter and
evidence store.  Pydantic is used at this boundary so malformed model output
cannot silently become a browser command.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_GOAL_LENGTH = 2_000
MAX_TEXT_LENGTH = 20_000
MAX_STEPS = 500
MAX_TIMEOUT_SECONDS = 3_600.0


def utc_now() -> datetime:
    """Return an aware UTC timestamp used by run records."""

    return datetime.now(UTC)


class FailureKind(StrEnum):
    """Stable failure categories consumed by the API and dashboard."""

    NONE = "none"
    ENVIRONMENT = "environment"
    CONNECTION = "connection"
    ACTION = "action"
    ASSERTION = "assertion"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MODEL = "model"
    UNKNOWN = "unknown"


class RuntimeState(StrEnum):
    """Observable states of one single-agent run."""

    RECEIVED = "received"
    OBSERVING = "observing"
    ACTING = "acting"
    VERIFYING = "verifying"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class AssertionType(StrEnum):
    TEXT = "text"
    ELEMENT_STATE = "element_state"
    STRUCTURED = "structured"


class DomainModel(BaseModel):
    """Common model configuration for public domain contracts."""

    model_config = ConfigDict(
        extra="ignore",
        arbitrary_types_allowed=True,
        validate_assignment=True,
        use_enum_values=True,
    )


class RetryPolicy(DomainModel):
    """Bounded retry policy for one action.

    ``idempotent_actions_only`` is intentionally true by default.  A caller
    must explicitly opt into retrying actions with side effects.
    """

    max_retries: int = Field(default=1, ge=0, le=20)
    idempotent_actions_only: bool = True
    retryable_failure_kinds: tuple[str, ...] = (
        FailureKind.ENVIRONMENT.value,
        FailureKind.CONNECTION.value,
        FailureKind.ACTION.value,
        FailureKind.TIMEOUT.value,
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_names(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "retries" in data and "max_retries" not in data:
            data["max_retries"] = data.pop("retries")
        if "retryable_actions_only" in data and "idempotent_actions_only" not in data:
            data["idempotent_actions_only"] = data.pop("retryable_actions_only")
        return data

    @field_validator("retryable_failure_kinds", mode="before")
    @classmethod
    def _normalise_failure_kinds(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = (value,)
        return tuple(_normalise_failure_kind(item) for item in value)


class Assertion(DomainModel):
    """A deterministic business-result assertion.

    ``type=text`` reads page text (or the selected element's text),
    ``type=element_state`` reads one property of a selected element, and
    ``type=structured`` resolves a dotted path in ``Observation.structured``.
    """

    type: str = Field(
        default=AssertionType.TEXT.value,
        validation_alias=AliasChoices("type", "assertion_type", "kind"),
    )
    name: str | None = None
    selector: str | None = None
    field: str | None = None
    expected: Any = None
    operator: str | None = None
    case_sensitive: bool = True

    @model_validator(mode="before")
    @classmethod
    def _normalise_input(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"type": AssertionType.TEXT.value, "expected": value}
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "assertion_type" in data and "type" not in data and "kind" not in data:
            data["type"] = data.pop("assertion_type")
        if "path" in data and "field" not in data:
            data["field"] = data.pop("path")
        return data

    @field_validator("type", mode="before")
    @classmethod
    def _normalise_type(cls, value: Any) -> str:
        normalised = str(value or AssertionType.TEXT.value).strip().lower().replace("-", "_")
        aliases = {
            "element": AssertionType.ELEMENT_STATE.value,
            "elementstate": AssertionType.ELEMENT_STATE.value,
            "element_status": AssertionType.ELEMENT_STATE.value,
            "json": AssertionType.STRUCTURED.value,
            "result": AssertionType.STRUCTURED.value,
        }
        normalised = aliases.get(normalised, normalised)
        if normalised not in {item.value for item in AssertionType}:
            raise ValueError(f"unsupported assertion type: {value!r}")
        return normalised

    @field_validator("operator", mode="before")
    @classmethod
    def _normalise_operator(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip().lower().replace("-", "_")

    @model_validator(mode="after")
    def _set_default_operator(self) -> Assertion:
        if self.operator is not None:
            return self
        default = {
            AssertionType.TEXT.value: "contains",
            AssertionType.ELEMENT_STATE.value: "equals",
            AssertionType.STRUCTURED.value: "equals",
        }[self.type]
        # Mutating only during Pydantic construction avoids returning a copy
        # from a top-level ``model_validator`` (which Pydantic warns about
        # when the model is instantiated through ``__init__``).
        object.__setattr__(self, "operator", default)
        return self

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        target = self.selector or self.field or "page"
        return f"{self.type}:{target}"


# The longer name is useful to callers that prefer explicit contracts.
AssertionSpec = Assertion


class ElementState(DomainModel):
    """Small, bounded snapshot of an interactive DOM element."""

    exists: bool = True
    visible: bool | None = None
    enabled: bool | None = None
    checked: bool | None = None
    text: str | None = None
    value: Any = None
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_primitive(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            data = dict(value)
            # CDP snapshots commonly expose ``disabled`` rather than the
            # positive ``enabled`` field used by the assertion DSL.
            if "enabled" not in data and "disabled" in data:
                data["enabled"] = not bool(data["disabled"])
            if "text" not in data and "name" in data:
                data["text"] = data["name"]
            if "visible" not in data and "exists" in data:
                data["visible"] = bool(data["exists"])
            return data
        if value is None:
            return {"exists": False}
        return {"text": str(value)}


class Observation(DomainModel):
    """The bounded page state supplied to the model and assertion engine."""

    url: str = ""
    title: str = ""
    text: str = Field(
        default="",
        validation_alias=AliasChoices("text", "page_summary", "visible_text", "page_text"),
    )
    elements: dict[str, ElementState] = Field(default_factory=dict)
    structured: dict[str, Any] = Field(default_factory=dict)
    tab_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text", mode="before")
    @classmethod
    def _coerce_text(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value)[:MAX_TEXT_LENGTH]

    @field_validator("elements", mode="before")
    @classmethod
    def _coerce_elements(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return {str(key): item for key, item in value.items()}
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            result: dict[str, Any] = {}
            for index, item in enumerate(value):
                if isinstance(item, Mapping):
                    key = item.get("selector") or item.get("id") or item.get("name") or str(index)
                    result[str(key)] = item
                else:
                    result[str(index)] = item
            return result
        raise TypeError("elements must be a mapping or sequence")


class Action(DomainModel):
    """A constrained browser operation chosen by the model."""

    kind: str = Field(
        validation_alias=AliasChoices("kind", "name", "action", "type"),
    )
    target: str | None = None
    value: Any = None
    url: str | None = None
    condition: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    idempotent: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("idempotent", "is_idempotent"),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalise_input(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"kind": value}
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        if "arguments" in data and "args" not in data:
            data["args"] = data.pop("arguments")
        if "params" in data and "args" not in data:
            data["args"] = data.pop("params")
        if "selector" in data and "target" not in data:
            data["target"] = data["selector"]
        if "element" in data and "target" not in data:
            data["target"] = data["element"]
        if "text" in data and "value" not in data:
            data["value"] = data["text"]
        aliases = {
            "navigate": "goto",
            "navigation": "goto",
            "open": "goto",
            "fill": "type",
            "fill_input": "type",
            "input": "type",
            "write": "type",
            "sleep": "wait",
            "wait_for_element": "wait",
            "wait_for_load": "wait",
            "press_key": "key",
            "capture_screenshot": "screenshot",
            "done": "finish",
            "complete": "finish",
            "stop": "finish",
        }
        kind = data.get("kind", data.get("name", data.get("action", data.get("type"))))
        if isinstance(kind, str):
            data["kind"] = aliases.get(kind.strip().lower().replace("-", "_"), kind)
        return data

    @field_validator("kind", mode="before")
    @classmethod
    def _normalise_kind(cls, value: Any) -> str:
        if value is None or not str(value).strip():
            raise ValueError("action kind must not be empty")
        return str(value).strip().lower().replace("-", "_")

    @field_validator("args", mode="before")
    @classmethod
    def _coerce_args(cls, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeError("action args must be a mapping")
        return dict(value)

    @model_validator(mode="after")
    def _infer_idempotency(self) -> Action:
        if self.idempotent is not None:
            return self
        safe = {"goto", "navigate", "wait", "scroll", "observe", "screenshot", "finish", "done", "stop"}
        object.__setattr__(self, "idempotent", self.kind in safe)
        return self

    @property
    def is_terminal(self) -> bool:
        return self.kind in {"finish", "done", "stop", "complete"}

    @property
    def name(self) -> str:
        return self.kind

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.args)

    @property
    def selector(self) -> str | None:
        return self.target


class ActionOutcome(DomainModel):
    """Normalised result returned by a Browser Adapter action."""

    success: bool = Field(default=True, validation_alias=AliasChoices("success", "ok"))
    observation: Observation | None = None
    error: str | None = None
    failure_kind: str = FailureKind.NONE.value
    duration_seconds: float = Field(default=0.0, ge=0.0)
    evidence: dict[str, Any] = Field(default_factory=dict)
    retryable: bool | None = None
    status: str | None = None
    value: Any = None

    @model_validator(mode="before")
    @classmethod
    def _normalise_status(cls, value: Any) -> Any:
        if value is None:
            return {}
        if isinstance(value, bool):
            return {"success": value}
        if not isinstance(value, Mapping):
            return {"success": True, "evidence": {"result": value}}
        data = dict(value)
        if "status" in data and "success" not in data and "ok" not in data:
            data["success"] = str(data.pop("status")).lower() in {"ok", "success", "succeeded", "passed"}
        if "error_message" in data and "error" not in data:
            data["error"] = data.pop("error_message")
        return data

    @field_validator("failure_kind", mode="before")
    @classmethod
    def _normalise_kind(cls, value: Any) -> str:
        return _normalise_failure_kind(value)

    @model_validator(mode="after")
    def _derive_failure_kind(self) -> ActionOutcome:
        if self.status is None:
            object.__setattr__(self, "status", "success" if self.success else "failed")
        if self.success:
            object.__setattr__(self, "failure_kind", FailureKind.NONE.value)
            return self
        if self.failure_kind == FailureKind.NONE.value:
            object.__setattr__(self, "failure_kind", FailureKind.ACTION.value)
            return self
        return self

    @property
    def error_summary(self) -> str | None:
        return self.error


class AssertionResult(DomainModel):
    """Result of evaluating one assertion against one observation."""

    name: str
    assertion_type: str
    passed: bool
    expected: Any = None
    actual: Any = None
    evidence: str = ""
    failure_kind: str = FailureKind.NONE.value
    error: str | None = None

    @field_validator("failure_kind", mode="before")
    @classmethod
    def _normalise_kind(cls, value: Any) -> str:
        return _normalise_failure_kind(value)

    @model_validator(mode="after")
    def _derive_failure_kind(self) -> AssertionResult:
        expected = FailureKind.NONE.value if self.passed else FailureKind.ASSERTION.value
        if self.failure_kind == FailureKind.NONE.value and not self.passed:
            object.__setattr__(self, "failure_kind", expected)
            return self
        return self

    @property
    def status(self) -> str:
        """Storage-friendly status while retaining the boolean ``passed`` API."""

        return "passed" if self.passed else "failed"


class StepTrace(DomainModel):
    """One attempted action and its page/verification evidence."""

    sequence: int = Field(ge=1)
    attempt: int = Field(default=1, ge=1)
    state: str = RuntimeState.ACTING.value
    observation: Observation | None = None
    action: Action | None = None
    outcome: ActionOutcome | None = None
    assertions: list[AssertionResult] = Field(default_factory=list)
    status: str = "success"
    failure_kind: str = FailureKind.NONE.value
    error: str | None = None
    duration_seconds: float = Field(default=0.0, ge=0.0)

    @field_validator("failure_kind", mode="before")
    @classmethod
    def _normalise_kind(cls, value: Any) -> str:
        return _normalise_failure_kind(value)

    @property
    def helper(self) -> str | None:
        return self.action.kind if self.action else None

    @property
    def args_summary(self) -> dict[str, Any]:
        return self.action.args if self.action else {}


# Names used by the storage layer and docs.  They intentionally point at the
# same immutable contract rather than introducing a second, drifting schema.
StepRecord = StepTrace


class AgentResult(DomainModel):
    """Terminal result of one bounded single-agent run."""

    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    status: str = "failed"
    failure_kind: str = FailureKind.UNKNOWN.value
    answer_summary: str = ""
    steps: list[StepTrace] = Field(default_factory=list)
    assertions: list[AssertionResult] = Field(default_factory=list)
    first_deviation_step: int | None = Field(default=None, ge=1)
    state_history: list[str] = Field(default_factory=list)
    termination_reason: str | None = None
    steps_used: int = Field(default=0, ge=0)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime = Field(default_factory=utc_now)
    duration_seconds: float = Field(default=0.0, ge=0.0)

    @field_validator("failure_kind", mode="before")
    @classmethod
    def _normalise_kind(cls, value: Any) -> str:
        return _normalise_failure_kind(value, default=FailureKind.UNKNOWN.value)

    @property
    def succeeded(self) -> bool:
        return self.status == "success" and self.failure_kind == FailureKind.NONE.value

    @property
    def success(self) -> bool:
        return self.succeeded


def _normalise_failure_kind(value: Any, default: str = FailureKind.UNKNOWN.value) -> str:
    if value is None or str(value).strip() == "":
        return default
    text = str(value).strip().lower().replace("-", "_")
    aliases = {
        "ok": FailureKind.NONE.value,
        "success": FailureKind.NONE.value,
        "succeeded": FailureKind.NONE.value,
        "env": FailureKind.ENVIRONMENT.value,
        "cdp": FailureKind.CONNECTION.value,
        "websocket": FailureKind.CONNECTION.value,
        "browser": FailureKind.ENVIRONMENT.value,
        "exec": FailureKind.ACTION.value,
        "verification": FailureKind.ASSERTION.value,
        "llm": FailureKind.MODEL.value,
    }
    return aliases.get(text, text if text in {kind.value for kind in FailureKind} else default)


def _resolve_path(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for component in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(component)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)) and component.isdigit():
            index = int(component)
            current = current[index] if index < len(current) else None
        else:
            current = getattr(current, component, None)
        if current is None:
            break
    return current


def _compare(actual: Any, expected: Any, operator: str, case_sensitive: bool = True) -> bool:
    operator = (operator or "equals").lower().replace("-", "_")
    left, right = actual, expected
    if not case_sensitive and isinstance(left, str) and isinstance(right, str):
        left, right = left.casefold(), right.casefold()
    if operator in {"equals", "equal", "eq", "is"}:
        return left == right
    if operator in {"not_equals", "not_equal", "neq", "is_not"}:
        return left != right
    if operator in {"contains", "includes", "has"}:
        if left is None:
            return False
        if isinstance(left, Mapping):
            return right in left
        if isinstance(left, (list, tuple, set, str)):
            return right in left
        return str(right) in str(left)
    if operator in {"not_contains", "excludes"}:
        return not _compare(left, right, "contains", case_sensitive)
    if operator == "starts_with":
        return isinstance(left, str) and left.startswith(str(right))
    if operator == "ends_with":
        return isinstance(left, str) and left.endswith(str(right))
    if operator in {"exists", "present"}:
        return left is not None and left is not False
    if operator in {"truthy", "true"}:
        return bool(left) is True
    if operator in {"falsy", "false"}:
        return bool(left) is False
    if operator == "regex":
        return isinstance(left, str) and re.search(str(right), left) is not None
    raise ValueError(f"unsupported assertion operator: {operator!r}")


def evaluate_assertion(assertion: Assertion | Mapping[str, Any], observation: Observation | Mapping[str, Any]) -> AssertionResult:
    """Evaluate one deterministic assertion and return bounded evidence.

    A malformed assertion is represented as a failed assertion rather than
    raised out of the runtime, so a task receives a useful ``failure_kind``.
    Schema errors at the API boundary are still rejected by ``TaskSpec``.
    """

    try:
        spec = assertion if isinstance(assertion, Assertion) else Assertion.model_validate(assertion)
        page = observation if isinstance(observation, Observation) else Observation.model_validate(observation)
        actual: Any
        if spec.type == AssertionType.TEXT.value:
            if spec.selector:
                element = page.elements.get(spec.selector)
                actual = element.text if element else None
            else:
                actual = page.text
        elif spec.type == AssertionType.ELEMENT_STATE.value:
            element = page.elements.get(spec.selector or "")
            actual = None if element is None else _resolve_path(element, spec.field or "visible")
        else:
            actual = _resolve_path(page.structured, spec.field)
        passed = _compare(actual, spec.expected, spec.operator or "equals", spec.case_sensitive)
        evidence = f"actual={_bounded_repr(actual)}; expected={_bounded_repr(spec.expected)}"
        return AssertionResult(
            name=spec.display_name,
            assertion_type=spec.type,
            passed=passed,
            expected=spec.expected,
            actual=actual,
            evidence=evidence[:1_000],
        )
    except Exception as exc:  # noqa: BLE001 - assertion failures are data
        name = assertion.display_name if isinstance(assertion, Assertion) else "invalid-assertion"
        return AssertionResult(
            name=name,
            assertion_type=getattr(assertion, "type", "unknown"),
            passed=False,
            evidence="",
            failure_kind=FailureKind.ASSERTION.value,
            error=str(exc)[:500],
        )


def evaluate_assertions(assertions: Sequence[Assertion | Mapping[str, Any]], observation: Observation) -> list[AssertionResult]:
    """Evaluate all assertions in declaration order."""

    return [evaluate_assertion(assertion, observation) for assertion in assertions]


def _bounded_repr(value: Any, limit: int = 400) -> str:
    try:
        text = repr(value)
    except BaseException as _exc:  # noqa: BLE001  # pragma: no cover - defensive for hostile objects
        text = "<unrepresentable>"
    return text[:limit]


__all__ = [
    "Action",
    "ActionOutcome",
    "AgentResult",
    "Assertion",
    "AssertionResult",
    "AssertionSpec",
    "AssertionType",
    "DomainModel",
    "ElementState",
    "FailureKind",
    "Observation",
    "RetryPolicy",
    "RunStatus",
    "RuntimeState",
    "StepRecord",
    "StepTrace",
    "TaskSpec",
    "evaluate_assertion",
    "evaluate_assertions",
]


class RunStatus(StrEnum):
    """Final task statuses (kept separate from intermediate RuntimeState)."""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class TaskSpec(DomainModel):
    """Validated input contract for a single-agent task."""

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    goal: str = Field(min_length=1, max_length=MAX_GOAL_LENGTH)
    assertions: list[Assertion] = Field(default_factory=list)
    max_steps: int = Field(default=30, ge=1, le=MAX_STEPS)
    timeout_seconds: float = Field(default=120.0, gt=0.0, le=MAX_TIMEOUT_SECONDS)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flatten_constraints(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        constraints = data.pop("constraints", None)
        if isinstance(constraints, Mapping):
            for key in ("max_steps", "timeout_seconds", "retry_policy"):
                if key not in data and key in constraints:
                    data[key] = constraints[key]
            if "retry_policy" not in data:
                retry_fields = {key: constraints[key] for key in ("max_retries", "idempotent_actions_only") if key in constraints}
                if retry_fields:
                    data["retry_policy"] = retry_fields
        if "name" in data and "task_id" not in data:
            data["task_id"] = data.pop("name")
        return data
