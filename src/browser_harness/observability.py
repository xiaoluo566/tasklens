"""TaskLens evidence contracts and bounded redaction helpers.

The browser harness produces transient observations and helper traces.  This
module turns those values into a small, JSON-compatible evidence contract that
can be handed to a repository or an API without leaking credentials.  It is
deliberately independent of the CDP and Agent Runtime implementations.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Hard limits are part of the data contract.  They protect both SQLite and the
# model context when a page returns an unexpectedly large document.
MAX_GOAL_LENGTH = 240
MAX_TASK_PREVIEW_LENGTH = 240
MAX_OUTPUT_LENGTH = 2_000
MAX_OBSERVATION_LENGTH = 2_000
MAX_ACTION_LENGTH = 120
MAX_ARGS_LENGTH = 300
MAX_ERROR_LENGTH = 1_000
MAX_EVIDENCE_LENGTH = 500
MAX_STEPS_PER_RUN = 500
MAX_ASSERTIONS_PER_STEP = 100

REDACTED = "[REDACTED]"


class FailureKind(StrEnum):
    """Stable failure categories exposed by the API and Dashboard."""

    NONE = "none"
    ENVIRONMENT = "environment"
    CONNECTION = "connection"
    ACTION = "action"
    ASSERTION = "assertion"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    MODEL = "model"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class AssertionStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


_SENSITIVE_KEY = re.compile(
    r"(?:^|[_\-.])(?:token|password|passwd|cookie|authorization|auth|"
    r"api[_\-.]?key|secret|session(?:[_\-.]?id)?|access[_\-.]?token|"
    r"refresh[_\-.]?token|client[_\-.]?secret)(?:$|[_\-.])",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(\bBearer\s+)[^\s,;]+", re.IGNORECASE)
_KEY_VALUE = re.compile(
    r"(?P<prefix>(?:token|password|passwd|cookie|authorization|api[_\-.]?key|"
    r"secret|session(?:[_\-.]?id)?|access[_\-.]?token|refresh[_\-.]?token)"
    r"\s*[:=]\s*)(?P<value>[^\s,;&}\]]+)",
    re.IGNORECASE,
)
_JSON_KEY_VALUE = re.compile(
    r"(?P<quote>[\"']?)(?P<key>token|password|passwd|cookie|authorization|"
    r"api[_\-.]?key|apikey|secret|session(?:[_\-.]?id)?|access[_\-.]?token|"
    r"refresh[_\-.]?token)(?P=quote)\s*:\s*"
    r"(?P<value_quote>[\"'])(?P<value>.*?)(?P=value_quote)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _bounded(value: Any, limit: int) -> str:
    """Convert a value to text defensively and cap it to *limit* characters."""

    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:  # noqa: BLE001  # pragma: no cover - hostile __str__
        text = "<unserializable>"
    return text[: max(0, limit)]


def sanitize_url(value: Any) -> str:
    """Return a URL without query, fragment, or user-info credentials.

    Query strings are removed wholesale rather than attempting to maintain an
    allow-list: analytics identifiers and unknown application parameters can
    also carry secrets.  Relative paths remain relative, which is useful for
    local test servers and keeps the evidence deterministic.
    """

    raw = _bounded(value, MAX_OUTPUT_LENGTH)
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        netloc = parts.hostname or ""
        if parts.port is not None:
            netloc = f"{netloc}:{parts.port}"
        # Preserve IPv6 bracket notation when urlsplit exposes a bare host.
        if ":" in netloc and not netloc.startswith("["):
            netloc = f"[{netloc}]"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        # A malformed URL is still bounded and should not make observation
        # itself fatal.  Strip obvious query/fragment portions as a fallback.
        return re.split(r"[?#]", raw, maxsplit=1)[0]


def _redact_text(value: Any) -> str:
    try:
        text = str(value)
    except Exception:  # noqa: BLE001  # pragma: no cover - hostile __str__
        text = "<unserializable>"
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _JSON_KEY_VALUE.sub(
        lambda match: (
            f"{match.group('quote')}{match.group('key')}{match.group('quote')}:"
            f"{match.group('value_quote')}{REDACTED}{match.group('value_quote')}"
        ),
        text,
    )
    text = _KEY_VALUE.sub(lambda match: f"{match.group('prefix')}{REDACTED}", text)

    def replace_url(match: re.Match[str]) -> str:
        return sanitize_url(match.group(0))

    return _URL.sub(replace_url, text)


def sanitize_text(value: Any, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Redact common credentials in arbitrary text and enforce a hard limit."""

    return _redact_text(value)[: max(0, max_length)] if value is not None else ""


def _is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9_.-]+", "_", str(key).lower())
    # Match exact names as well as compound names such as ``request_token``.
    return bool(_SENSITIVE_KEY.search(normalized)) or normalized in {
        "token",
        "password",
        "passwd",
        "cookie",
        "authorization",
        "auth",
        "secret",
        "session",
        "session_id",
        "api_key",
        "apikey",
    }


def sanitize_mapping(value: Any, *, max_length: int | None = None) -> Any:
    """Recursively redact mapping/list values while preserving JSON types.

    Sensitive key values are replaced before descending into them.  Non-JSON
    objects are represented by bounded text; this function never raises for a
    malformed observation.
    """

    def visit(item: Any, key: Any = None, *, redact_action_value: bool = False) -> Any:
        if key is not None and _is_sensitive_key(key):
            return REDACTED
        if isinstance(item, Mapping):
            kind = str(item.get("kind", item.get("action", item.get("type", "")))).lower()
            is_type_action = redact_action_value or kind in {"type", "fill", "fill_input", "input", "write"}
            return {
                str(k): (
                    REDACTED
                    if is_type_action and str(k).lower() in {"value", "text"}
                    else visit(v, k, redact_action_value=is_type_action)
                )
                for k, v in item.items()
            }
        if isinstance(item, (list, tuple, set)):
            return [visit(v, redact_action_value=redact_action_value) for v in item]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        text = _redact_text(item)
        return text if max_length is None else text[: max(0, max_length)]

    return visit(value)


def sanitize_value(value: Any, *, max_length: int = MAX_OUTPUT_LENGTH) -> Any:
    """Sanitize an arbitrary JSON-like value and bound serialized text.

    This helper is used for assertion expected/actual values.  Structured
    values retain their shape; scalar strings are bounded after redaction.
    """

    safe = sanitize_mapping(value, max_length=max_length)
    if isinstance(safe, str):
        return safe[:max_length]
    return safe


def _json_safe(value: Any) -> Any:
    """Return a value accepted by ``json.dumps`` without invoking surprises."""

    safe = sanitize_mapping(value, max_length=MAX_OUTPUT_LENGTH)
    try:
        json.dumps(safe, ensure_ascii=False)
        return safe
    except (TypeError, ValueError):
        return sanitize_text(safe, MAX_OUTPUT_LENGTH)


class EvidenceModel(BaseModel):
    """Common Pydantic configuration for evidence records."""

    model_config = ConfigDict(
        extra="ignore",
        use_enum_values=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class AssertionRecord(EvidenceModel):
    """Result of one business assertion independent of helper success."""

    name: str = Field(min_length=1, max_length=120)
    assertion_type: str = Field(default="text", alias="type", max_length=40)
    status: AssertionStatus = AssertionStatus.UNKNOWN
    operator: str | None = Field(default=None, max_length=40)
    expected: Any = None
    actual: Any = None
    evidence: Any = None
    error_summary: str | None = None

    @field_validator("name", "assertion_type", "operator", mode="before")
    @classmethod
    def _bounded_labels(cls, value: Any) -> Any:
        return _bounded(value, 120 if value is not None else 0)

    @field_validator("expected", "actual", "evidence", mode="before")
    @classmethod
    def _safe_values(cls, value: Any) -> Any:
        return sanitize_value(value, max_length=MAX_EVIDENCE_LENGTH)

    @field_validator("error_summary", mode="before")
    @classmethod
    def _safe_error(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_text(value, MAX_ERROR_LENGTH)

    @property
    def type(self) -> str:
        """Compatibility accessor for the JSON contract's ``type`` key."""

        return self.assertion_type


class StepRecord(EvidenceModel):
    """One helper/action execution and its post-action assertions."""

    sequence: int = Field(ge=1)
    helper: str = Field(default="unknown", max_length=MAX_ACTION_LENGTH)
    observation_summary: str = Field(default="", max_length=MAX_OBSERVATION_LENGTH)
    action: str = Field(default="", max_length=MAX_ACTION_LENGTH)
    args_summary: str = Field(default="", max_length=MAX_ARGS_LENGTH)
    duration_seconds: float | None = Field(default=None, ge=0)
    status: StepStatus = StepStatus.RUNNING
    failure_kind: FailureKind = FailureKind.NONE
    assertions: list[AssertionRecord] = Field(default_factory=list)
    error_summary: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("helper", "action", mode="before")
    @classmethod
    def _safe_action_name(cls, value: Any) -> str:
        return sanitize_text(value, MAX_ACTION_LENGTH)

    @field_validator("observation_summary", mode="before")
    @classmethod
    def _safe_observation(cls, value: Any) -> str:
        return sanitize_text(value, MAX_OBSERVATION_LENGTH)

    @field_validator("args_summary", mode="before")
    @classmethod
    def _safe_args(cls, value: Any) -> str:
        if isinstance(value, (Mapping, list, tuple)):
            try:
                value = json.dumps(sanitize_mapping(value, max_length=MAX_ARGS_LENGTH), ensure_ascii=False)
            except (TypeError, ValueError):
                value = str(value)
        return sanitize_text(value, MAX_ARGS_LENGTH)

    @field_validator("error_summary", mode="before")
    @classmethod
    def _safe_error(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_text(value, MAX_ERROR_LENGTH)

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _safe_refs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return [sanitize_text(item, MAX_EVIDENCE_LENGTH) for item in list(value)[:MAX_ASSERTIONS_PER_STEP]]

    @field_validator("assertions", mode="before")
    @classmethod
    def _bound_assertions(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        return list(value)[:MAX_ASSERTIONS_PER_STEP]


class RunRecord(EvidenceModel):
    """Top-level bounded evidence for one TaskLens execution."""

    run_id: str = Field(min_length=1, max_length=128, alias="id")
    task_name: str = Field(default="", max_length=MAX_TASK_PREVIEW_LENGTH)
    goal: str = Field(default="", max_length=MAX_GOAL_LENGTH)
    task_preview: str = Field(default="", max_length=MAX_TASK_PREVIEW_LENGTH)
    task_length: int | None = Field(default=None, ge=0)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    status: RunStatus = RunStatus.RUNNING
    failure_kind: FailureKind = FailureKind.NONE
    first_deviation_step: int | None = Field(default=None, ge=1)
    browser_backend: str = Field(default="unknown", max_length=40)
    exit_code: int | None = None
    output_tail: str = Field(default="", max_length=MAX_OUTPUT_LENGTH)
    error_summary: str | None = None
    answer_summary: str = Field(default="", max_length=MAX_OUTPUT_LENGTH)
    max_steps: int | None = Field(default=None, ge=1)
    steps: list[StepRecord] = Field(default_factory=list)

    @field_validator("task_name", "task_preview", "goal", mode="before")
    @classmethod
    def _safe_task_text(cls, value: Any) -> str:
        return sanitize_text(value, MAX_TASK_PREVIEW_LENGTH)

    @field_validator("output_tail", "answer_summary", mode="before")
    @classmethod
    def _safe_output(cls, value: Any) -> str:
        return sanitize_text(value, MAX_OUTPUT_LENGTH)

    @field_validator("error_summary", mode="before")
    @classmethod
    def _safe_error(cls, value: Any) -> str | None:
        if value is None:
            return None
        return sanitize_text(value, MAX_ERROR_LENGTH)

    @field_validator("browser_backend", mode="before")
    @classmethod
    def _safe_backend(cls, value: Any) -> str:
        return sanitize_text(value or "unknown", 40)

    @field_validator("steps", mode="before")
    @classmethod
    def _bound_steps(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        return list(value)[:MAX_STEPS_PER_RUN]

    @model_validator(mode="after")
    def _derive_lengths(self) -> RunRecord:
        # Keep original task length when supplied, otherwise derive it from
        # the pre-redaction goal only as an approximation.  This is metadata,
        # not a copy of the original task.
        if self.task_length is None:
            object.__setattr__(self, "task_length", len(self.goal))
        if not self.task_preview:
            object.__setattr__(self, "task_preview", self.goal[:MAX_TASK_PREVIEW_LENGTH])
        return self

    @property
    def step_count(self) -> int:
        return len(self.steps)


def find_first_deviation_step(steps: Sequence[StepRecord]) -> int | None:
    """Return the earliest observable failed action or assertion sequence.

    This intentionally reports the first *observable* deviation, not a claim
    about the model's hidden root cause.  ``None`` means evidence is
    insufficient to locate a deviation.
    """

    for raw_step in steps:
        step = raw_step if isinstance(raw_step, StepRecord) else StepRecord.model_validate(raw_step)
        if step.status == StepStatus.FAILED or step.failure_kind != FailureKind.NONE:
            return step.sequence
        if any(assertion.status == AssertionStatus.FAILED for assertion in step.assertions):
            return step.sequence
    return None


def build_run_record(value: RunRecord | Mapping[str, Any]) -> RunRecord:
    """Normalize a runtime result into a bounded ``RunRecord``."""

    record = value if isinstance(value, RunRecord) else RunRecord.model_validate(value)
    if record.first_deviation_step is None and record.steps:
        deviation = find_first_deviation_step(record.steps)
        if deviation is not None:
            return record.model_copy(update={"first_deviation_step": deviation})
    return record


__all__ = [
    "AssertionRecord",
    "AssertionStatus",
    "EvidenceModel",
    "FailureKind",
    "RunRecord",
    "RunStatus",
    "StepRecord",
    "StepStatus",
    "build_run_record",
    "find_first_deviation_step",
    "sanitize_mapping",
    "sanitize_text",
    "sanitize_url",
    "sanitize_value",
]
