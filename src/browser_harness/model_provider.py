"""Controlled model providers for the TaskLens single-agent runtime.

The runtime accepts a very small ``next_action`` protocol.  This module keeps
provider concerns separate from browser control: a deterministic scripted
provider makes local demos and tests reproducible, while the optional
OpenAI-compatible provider translates a bounded page snapshot into one
validated browser action.  Model output is treated as untrusted data and is
never evaluated as Python or JavaScript.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

from .browser_adapter import InvalidActionError, normalize_action
from .observability import sanitize_mapping, sanitize_text, sanitize_url
from .tasklens_domain import Action, FailureKind, Observation, StepTrace, TaskSpec

MAX_PROVIDER_GOAL = 1_200
MAX_PROVIDER_OBSERVATION = 6_000
MAX_PROVIDER_HISTORY = 6_000
MAX_PROVIDER_HISTORY_ITEMS = 20
MAX_PROVIDER_RESPONSE = 1_000_000
MAX_PROVIDER_MESSAGE = 1_000
DEFAULT_PROVIDER_TIMEOUT = 60.0
ALLOWED_ACTIONS = frozenset(
    {"goto", "click", "type", "scroll", "wait", "key", "screenshot", "finish"}
)


def _failure_kind(value: Any, default: str = FailureKind.MODEL.value) -> str:
    aliases = {
        "env": FailureKind.ENVIRONMENT.value,
        "http": FailureKind.CONNECTION.value,
        "network": FailureKind.CONNECTION.value,
        "llm": FailureKind.MODEL.value,
    }
    text = str(value or default).strip().lower().replace("-", "_")
    text = aliases.get(text, text)
    return text if text in {item.value for item in FailureKind} else default


class ProviderError(RuntimeError):
    """A bounded provider error carrying a runtime failure category."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: str = FailureKind.MODEL.value,
        retryable: bool = False,
    ) -> None:
        self.failure_kind = _failure_kind(failure_kind)
        self.retryable = bool(retryable)
        # Provider errors can be returned by the CLI/API, so redact before the
        # exception is constructed.  Never include request headers or bodies.
        super().__init__(sanitize_text(message, MAX_PROVIDER_MESSAGE))


class JsonHttpClient(Protocol):
    """Small injectable HTTP surface used by the OpenAI-compatible provider."""

    def post(
        self,
        url: str,
        *,
        json: Mapping[str, Any],
        headers: Mapping[str, str],
        timeout: float,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """Validated environment-backed settings without retaining secrets."""

    base_url: str
    model: str
    timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT


class ScriptedModelProvider:
    """Deterministic provider for tests, screenshots, and local smoke runs."""

    def __init__(
        self,
        actions: Sequence[Action | Mapping[str, Any] | str] = (),
        *,
        finish_when_exhausted: bool = False,
    ) -> None:
        # Copy caller-owned mappings once; callers cannot mutate the script
        # while a run is consuming it.
        self.actions = tuple(deepcopy(tuple(actions)))
        self.finish_when_exhausted = bool(finish_when_exhausted)
        self._index = 0

    async def next_action(
        self,
        task: TaskSpec,
        observation: Observation,
        history: Sequence[StepTrace],
    ) -> Action:
        del task, observation, history
        if self._index >= len(self.actions):
            if self.finish_when_exhausted:
                return Action(kind="finish", value="script exhausted")
            raise ProviderError("scripted provider has no remaining action", failure_kind="model")
        raw = deepcopy(self.actions[self._index])
        self._index += 1
        try:
            action = normalize_action(raw)
        except (InvalidActionError, TypeError, ValueError) as exc:
            raise ProviderError("scripted action is invalid", failure_kind="model") from exc
        _validate_action_kind(action)
        return action


class DemoModelProvider(ScriptedModelProvider):
    """A zero-network provider that emits one explicit terminal decision."""

    def __init__(self) -> None:
        super().__init__(
            actions=(Action(kind="finish", value="demo provider"),),
            finish_when_exhausted=True,
        )


class _HttpStatusError(RuntimeError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"provider returned HTTP {status}")


def _urllib_post(
    url: str,
    *,
    payload: Mapping[str, Any],
    headers: Mapping[str, str],
    timeout: float,
) -> Mapping[str, Any]:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_PROVIDER_RESPONSE + 1)
            if len(raw) > MAX_PROVIDER_RESPONSE:
                raise ProviderError("provider response is too large", failure_kind="model")
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, Mapping):
                raise ProviderError("provider response must be a JSON object", failure_kind="model")
            return dict(parsed)
    except urllib.error.HTTPError as exc:
        raise _HttpStatusError(int(exc.code)) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ProviderError("provider connection failed", failure_kind="connection", retryable=True) from exc
    except json.JSONDecodeError as exc:
        raise ProviderError("provider returned invalid JSON", failure_kind="model") from exc


class OpenAICompatibleProvider:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint safely.

    ``http_client`` is intentionally injectable.  It may expose an async or
    sync ``post`` method with the ``JsonHttpClient`` signature; production uses
    a stdlib urllib transport so the runtime dependency set stays small.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT,
        http_client: JsonHttpClient | None = None,
    ) -> None:
        self.endpoint = _validate_endpoint(base_url)
        self.api_key = str(api_key or "").strip()
        self.model = _validate_model(model)
        self.timeout_seconds = _validate_timeout(timeout_seconds)
        parts = urlsplit(self.endpoint)
        if parts.scheme != "https" and not _is_loopback(parts.hostname):
            raise ProviderError(
                "non-loopback HTTP model endpoints are not allowed",
                failure_kind="environment",
            )
        if not self.api_key and not _is_loopback(parts.hostname):
            raise ProviderError("TASKLENS_LLM_API_KEY is required for remote endpoints", failure_kind="environment")
        self.http_client = http_client

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        http_client: JsonHttpClient | None = None,
    ) -> OpenAICompatibleProvider:
        values = dict(os.environ if environ is None else environ)
        base_url = str(values.get("TASKLENS_LLM_BASE_URL", "")).strip()
        model = str(values.get("TASKLENS_LLM_MODEL", "")).strip()
        if not base_url or not model:
            raise ProviderError(
                "configure TASKLENS_LLM_BASE_URL and TASKLENS_LLM_MODEL",
                failure_kind="environment",
            )
        raw_timeout = values.get("TASKLENS_LLM_TIMEOUT_SECONDS", str(DEFAULT_PROVIDER_TIMEOUT))
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise ProviderError("TASKLENS_LLM_TIMEOUT_SECONDS is invalid", failure_kind="environment") from exc
        return cls(
            base_url=base_url,
            api_key=values.get("TASKLENS_LLM_API_KEY"),
            model=model,
            timeout_seconds=timeout,
            http_client=http_client,
        )

    async def next_action(
        self,
        task: TaskSpec,
        observation: Observation,
        history: Sequence[StepTrace],
    ) -> Action:
        payload = self._build_payload(task, observation, history)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = await self._post(payload, headers)
            body = _response_body(response)
            content = _extract_response_content(body)
            candidate = _parse_action_json(content)
            action = normalize_action(candidate)
            _validate_action_kind(action)
            return action
        except ProviderError:
            raise
        except _HttpStatusError as exc:
            kind = "environment" if 400 <= exc.status < 500 else "connection"
            raise ProviderError(
                f"provider returned HTTP {exc.status}",
                failure_kind=kind,
                retryable=kind == "connection",
            ) from exc
        except TimeoutError as exc:
            raise ProviderError("provider request timed out", failure_kind="timeout", retryable=True) from exc
        except (ConnectionError, OSError, urllib.error.URLError) as exc:
            raise ProviderError("provider connection failed", failure_kind="connection", retryable=True) from exc
        except (InvalidActionError, TypeError, ValueError, KeyError, IndexError) as exc:
            raise ProviderError("provider returned an invalid action", failure_kind="model") from exc
        except Exception as exc:
            raise ProviderError("provider request failed", failure_kind="model") from exc

    def _build_payload(
        self,
        task: TaskSpec,
        observation: Observation,
        history: Sequence[StepTrace],
    ) -> dict[str, Any]:
        task_data = {
            "task_id": task.task_id,
            "goal": sanitize_text(task.goal, MAX_PROVIDER_GOAL),
            "assertions": sanitize_mapping(
                [item.model_dump(mode="json") for item in list(task.assertions)[:50]],
                max_length=MAX_PROVIDER_OBSERVATION,
            ),
            "max_steps": task.max_steps,
            "timeout_seconds": task.timeout_seconds,
        }
        observation_data = {
            "url": sanitize_url(observation.url),
            "title": sanitize_text(observation.title, 300),
            "text": sanitize_text(observation.text, MAX_PROVIDER_OBSERVATION),
            "elements": sanitize_mapping(observation.elements, max_length=MAX_PROVIDER_OBSERVATION),
            "structured": sanitize_mapping(observation.structured, max_length=MAX_PROVIDER_OBSERVATION),
        }
        history_data: list[dict[str, Any]] = []
        for step in list(history)[-MAX_PROVIDER_HISTORY_ITEMS:]:
            action = step.action.model_dump(mode="json") if step.action else None
            history_data.append(
                {
                    "sequence": step.sequence,
                    "attempt": step.attempt,
                    "state": step.state,
                    "action": sanitize_mapping(action, max_length=800),
                    "status": step.status,
                    "failure_kind": step.failure_kind,
                    "error": sanitize_text(step.error, 500),
                }
            )
        context = {
            "task": task_data,
            "observation": observation_data,
            "history": sanitize_mapping(history_data, max_length=MAX_PROVIDER_HISTORY),
        }
        user_content = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        return {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are the TaskLens browser test agent. Return exactly one JSON object "
                        "describing the next action. Allowed kinds: goto, click, type, scroll, "
                        "wait, key, screenshot, finish. Never return code or tool instructions."
                    ),
                },
                {"role": "user", "content": user_content[:MAX_PROVIDER_HISTORY + MAX_PROVIDER_OBSERVATION]},
            ],
        }

    async def _post(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> Any:
        client = self.http_client
        if client is None:
            return await asyncio.to_thread(
                _urllib_post,
                self.endpoint,
                payload=payload,
                headers=headers,
                timeout=self.timeout_seconds,
            )
        post = getattr(client, "post", None)
        if not callable(post):
            raise ProviderError("configured model client has no post method", failure_kind="environment")
        result = post(
            self.endpoint,
            json=payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        return await result if inspect.isawaitable(result) else result


def create_model_provider(
    environ: Mapping[str, str] | None = None,
    *,
    http_client: JsonHttpClient | None = None,
) -> ScriptedModelProvider | OpenAICompatibleProvider:
    """Build the configured provider without silently enabling a real LLM.

    ``TASKLENS_MODEL_PROVIDER=demo`` is an explicit, zero-network mode.  The
    default is OpenAI-compatible and fails with a clear environment error when
    its required settings are absent.
    """

    values = dict(os.environ if environ is None else environ)
    mode = str(values.get("TASKLENS_MODEL_PROVIDER", "openai")).strip().lower()
    if mode in {"demo", "scripted"}:
        return DemoModelProvider()
    if mode not in {"openai", "openai-compatible", "openai_compatible"}:
        raise ProviderError("TASKLENS_MODEL_PROVIDER is unsupported", failure_kind="environment")
    return OpenAICompatibleProvider.from_env(values, http_client=http_client)


def _validate_endpoint(value: str) -> str:
    text = str(value or "").strip().rstrip("/")
    if len(text) > 500:
        raise ProviderError("model endpoint is too long", failure_kind="environment")
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
    except ValueError as exc:
        raise ProviderError("model endpoint is malformed", failure_kind="environment") from exc
    if parts.scheme not in {"http", "https"} or not hostname or parts.username or parts.password:
        raise ProviderError("model endpoint must be an absolute HTTP URL", failure_kind="environment")
    if parts.query or parts.fragment:
        raise ProviderError("model endpoint must not contain query or fragment", failure_kind="environment")
    if text.endswith("/chat/completions"):
        return text
    return f"{text}/chat/completions"


def _validate_model(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 120 or any(char in text for char in "\r\n"):
        raise ProviderError("model name is invalid", failure_kind="environment")
    return text


def _validate_timeout(value: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ProviderError("model timeout is invalid", failure_kind="environment") from exc
    if not 1 <= timeout <= 300:
        raise ProviderError("model timeout must be between 1 and 300 seconds", failure_kind="environment")
    return timeout


def _is_loopback(hostname: str | None) -> bool:
    return str(hostname or "").lower().strip("[]") in {"localhost", "127.0.0.1", "::1"}


def _response_body(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    status = getattr(response, "status_code", 200)
    if isinstance(status, int) and status >= 400:
        raise _HttpStatusError(status)
    json_method = getattr(response, "json", None)
    if not callable(json_method):
        raise ProviderError("provider response is not JSON", failure_kind="model")
    body = json_method()
    if not isinstance(body, Mapping):
        raise ProviderError("provider response must be a JSON object", failure_kind="model")
    return body


def _extract_response_content(body: Mapping[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)) or not choices:
        raise ProviderError("provider response has no choices", failure_kind="model")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise ProviderError("provider response choice is invalid", failure_kind="model")
    message = choice.get("message")
    if not isinstance(message, Mapping):
        raise ProviderError("provider response message is invalid", failure_kind="model")
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes, bytearray)):
        for call in tool_calls:
            if isinstance(call, Mapping):
                function = call.get("function")
                if isinstance(function, Mapping) and function.get("arguments") is not None:
                    return sanitize_text(function.get("arguments"), MAX_PROVIDER_RESPONSE)
    content = message.get("content")
    if isinstance(content, str):
        return content[:MAX_PROVIDER_RESPONSE]
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes, bytearray)):
        parts = [item.get("text", "") for item in content if isinstance(item, Mapping)]
        return "".join(str(part) for part in parts)[:MAX_PROVIDER_RESPONSE]
    raise ProviderError("provider response contains no action content", failure_kind="model")


def _parse_action_json(content: str) -> Mapping[str, Any]:
    text = str(content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ProviderError("provider action is not valid JSON", failure_kind="model") from exc
    if isinstance(value, Mapping) and isinstance(value.get("action"), Mapping):
        value = value["action"]
    if not isinstance(value, Mapping):
        raise ProviderError("provider action must be a JSON object", failure_kind="model")
    return dict(value)


def _validate_action_kind(action: Action) -> None:
    if action.kind not in ALLOWED_ACTIONS:
        raise ProviderError("provider returned an unsupported action kind", failure_kind="model")


__all__ = [
    "DemoModelProvider",
    "JsonHttpClient",
    "OpenAICompatibleProvider",
    "ProviderError",
    "ProviderSettings",
    "ScriptedModelProvider",
    "create_model_provider",
]
