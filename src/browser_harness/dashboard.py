"""TaskLens read-only dashboard and task submission API.

The dashboard deliberately depends on small protocols instead of SQLite or the
Agent Runtime implementation.  This keeps the HTTP surface useful while the
runtime/storage slices evolve independently, and makes the API straightforward
to exercise with in-memory fakes.

Only ``POST /api/tasks`` has a side effect.  Historical run endpoints are
read-only and every response uses the same ``success/data/error/meta`` envelope.
"""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

MAX_RUN_LIMIT = 100
DEFAULT_RUN_LIMIT = 50
MAX_ASSERTION_VALUE_BYTES = 32_000
MAX_ASSERTION_VALUE_DEPTH = 8
MAX_ASSERTION_VALUE_ITEMS = 100
VALID_STATUSES = frozenset({"success", "failed", "timeout", "cancelled"})
VALID_BROWSERS = frozenset({"local", "cdp", "cloud", "unknown"})


class StoreProtocol(Protocol):
    """Minimal repository contract consumed by the dashboard.

    The concrete SQLite repository may expose ``browser_backend`` while test
    doubles often use ``browser``.  The adapter below supports both names.
    """

    def list_runs(self, **kwargs: Any) -> Sequence[Any]: ...

    def get_run(self, run_id: str) -> Any | None: ...

    def summary(self) -> Mapping[str, Any]: ...

    def health(self) -> Mapping[str, Any]: ...


class RuntimeProtocol(Protocol):
    async def run(self, task: Any) -> Any: ...


class TaskConstraints(BaseModel):
    """Resource limits accepted by the task endpoint."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=30, ge=1, le=500)
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)


class RetryPolicy(BaseModel):
    """Retry budget for safe/declared idempotent actions."""

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=0, ge=0, le=20)
    idempotent_actions_only: bool = True


class AssertionSpec(BaseModel):
    """Small, JSON-serialisable assertion DSL used by the runtime."""

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1, max_length=40)
    selector: str | None = Field(default=None, max_length=500)
    expected: Any = None
    name: str | None = Field(default=None, max_length=120)

    @field_validator("type")
    @classmethod
    def normalize_type(cls, value: str) -> str:
        normalized = value.strip().lower().replace("-", "_")
        aliases = {"element": "element_state", "elementstate": "element_state"}
        return aliases.get(normalized, normalized)

    @field_validator("expected")
    @classmethod
    def bound_expected_value(cls, value: Any) -> Any:
        """Reject oversized/deep assertion payloads before runtime creation."""

        def walk(item: Any, depth: int = 0) -> None:
            if depth > MAX_ASSERTION_VALUE_DEPTH:
                raise ValueError("assertion expected value is too deeply nested")
            if isinstance(item, Mapping):
                if len(item) > MAX_ASSERTION_VALUE_ITEMS:
                    raise ValueError("assertion expected object has too many fields")
                for key, nested in item.items():
                    walk(key, depth + 1)
                    walk(nested, depth + 1)
            elif isinstance(item, (list, tuple, set, frozenset)):
                if len(item) > MAX_ASSERTION_VALUE_ITEMS:
                    raise ValueError("assertion expected array has too many items")
                for nested in item:
                    walk(nested, depth + 1)

        walk(value)
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("assertion expected value must be JSON-serialisable") from exc
        if len(encoded.encode("utf-8")) > MAX_ASSERTION_VALUE_BYTES:
            raise ValueError("assertion expected value is too large")
        return value


class TaskRequest(BaseModel):
    """Public request model; converted to the runtime's TaskSpec contract."""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=2_000)
    assertions: list[AssertionSpec] = Field(default_factory=list, max_length=50)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @field_validator("task_id", "goal")
    @classmethod
    def trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class _MemoryStore:
    """Safe empty fallback used when the optional repository is unavailable."""

    def __init__(self, *, reason: str | None = None) -> None:
        self._runs: list[dict[str, Any]] = []
        self._reason = reason

    def health(self) -> dict[str, str | bool]:
        result: dict[str, str | bool] = {
            "ok": self._reason is None,
            "status": "ok" if self._reason is None else "degraded",
            "storage": "memory",
        }
        if self._reason:
            result["warning"] = self._reason
        return result

    def summary(self) -> dict[str, Any]:
        return {
            "total_runs": len(self._runs),
            "failed_count": sum(item.get("status") == "failed" for item in self._runs),
            "success_count": sum(item.get("status") == "success" for item in self._runs),
            "average_duration_seconds": _average_duration(self._runs),
        }

    def list_runs(
        self,
        *,
        limit: int = DEFAULT_RUN_LIMIT,
        status: str | None = None,
        browser_backend: str | None = None,
    ) -> list[dict[str, Any]]:
        runs = self._runs
        if status is not None:
            runs = [run for run in runs if run.get("status") == status]
        if browser_backend is not None:
            runs = [run for run in runs if run.get("browser_backend") == browser_backend]
        return runs[:limit]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return next((run for run in self._runs if run.get("run_id") == run_id), None)

    def append_result(self, result: Mapping[str, Any]) -> str:
        run = dict(result)
        run.setdefault("run_id", str(uuid4()))
        self._runs.append(run)
        return cast(str, run["run_id"])


def _average_duration(records: Sequence[Any]) -> float | None:
    values: list[float] = []
    for record in records:
        value = _as_mapping(record).get("duration_seconds")
        if isinstance(value, (int, float)):
            values.append(float(value))
    return round(sum(values) / len(values), 3) if values else None


def _as_mapping(value: Any) -> dict[str, Any]:
    """Convert Pydantic/dataclass/ORM records without invoking custom reprs."""

    if isinstance(value, BaseModel):
        return cast(dict[str, Any], value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return {str(key): item for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        dumped = to_dict()
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    if hasattr(value, "__dict__"):
        return {
            str(key): item
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return {"value": value}


def _jsonable(value: Any) -> Any:
    """Return a JSON-safe copy and avoid leaking arbitrary object internals."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # datetime/UUID and similar values have a stable string representation.
    return str(value)


def _envelope(
    *,
    success: bool,
    data: Any = None,
    error: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": success,
        "data": _jsonable(data),
        "error": _jsonable(error),
        "meta": _jsonable(meta or {}),
    }


def _error_response(
    status_code: int, code: str, message: str, *, details: Any = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = _jsonable(details)
    return JSONResponse(
        status_code=status_code,
        content=_envelope(success=False, error=error),
    )


def _validation_details(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in exc.errors():
        location = item.get("loc", ())
        field = ".".join(str(part) for part in location if part != "body") or "body"
        details.append(
            {
                "field": field,
                "message": str(item.get("msg", "invalid value")),
                "code": str(item.get("type", "invalid")),
            }
        )
    return details


def _storage_method(store: Any, names: Sequence[str]) -> Callable[..., Any] | None:
    for name in names:
        method = getattr(store, name, None)
        if callable(method):
            return cast(Callable[..., Any], method)
    return None


def _invoke_store_list(
    store: Any, *, limit: int, status: str | None, browser: str | None
) -> Sequence[Any]:
    method = _storage_method(store, ("list_runs", "list"))
    if method is None:
        return []
    kwargs = {"limit": limit, "status": status, "browser_backend": browser}
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        # A small number of repository adapters do not expose a signature.
        return cast(Sequence[Any], method(limit, status, browser))

    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if not accepts_kwargs:
        names = signature.parameters
        kwargs = {key: value for key, value in kwargs.items() if key in names}
        if "browser_backend" not in names and "browser" in names:
            kwargs["browser"] = browser
    try:
        signature.bind(**kwargs)
    except TypeError:
        try:
            signature.bind(limit, status, browser)
        except TypeError:
            # Preserve the original keyword call's useful boundary error.
            return cast(Sequence[Any], method(**kwargs))
        return cast(Sequence[Any], method(limit, status, browser))
    return cast(Sequence[Any], method(**kwargs))


def _invoke_store_detail(store: Any, run_id: str) -> Any | None:
    method = _storage_method(store, ("get_run", "find_run", "get"))
    return None if method is None else method(run_id)


def _invoke_store_summary(store: Any) -> Mapping[str, Any]:
    method = _storage_method(store, ("summary", "get_summary"))
    if method is None:
        return {"total_runs": 0, "failed_count": 0, "success_count": 0}
    result = method()
    return _as_mapping(result)


def _invoke_store_health(store: Any) -> Mapping[str, Any]:
    method = _storage_method(store, ("health", "get_health"))
    if method is None:
        return {"status": "ok", "storage": type(store).__name__}
    result = method()
    if isinstance(result, Mapping):
        return dict(result)
    return {"status": "ok" if result else "degraded", "storage": type(store).__name__}


def _normalize_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve repository keys while exposing the stable dashboard aliases."""

    data = dict(raw)
    total = data.get("total", data.get("total_runs", 0))
    failed = data.get("failed", data.get("failed_count", 0))
    succeeded = data.get("succeeded", data.get("success_count", data.get("successful_count", 0)))
    data.setdefault("total", total)
    data.setdefault("total_runs", total)
    data.setdefault("failed", failed)
    data.setdefault("failed_count", failed)
    data.setdefault("succeeded", succeeded)
    data.setdefault("success_count", succeeded)
    if "average_duration_seconds" not in data:
        data["average_duration_seconds"] = None
    return data


def _to_runtime_task(request: TaskRequest) -> Any:
    """Build the runtime TaskSpec when available, otherwise use the request model."""

    payload = {
        "task_id": request.task_id,
        "goal": request.goal,
        "assertions": [item.model_dump(mode="json") for item in request.assertions],
        "max_steps": request.constraints.max_steps,
        "timeout_seconds": request.constraints.timeout_seconds,
        "retry_policy": request.retry_policy.model_dump(mode="json"),
    }
    try:
        from .tasklens_domain import TaskSpec
    except ImportError:
        # Keep the HTTP layer usable in a minimal embedding that deliberately
        # omits the optional domain module.  In the normal package install the
        # import succeeds and all DSL validation happens below.
        return request
    if hasattr(TaskSpec, "model_validate"):
        return TaskSpec.model_validate(payload)
    return TaskSpec(**payload)  # pragma: no cover - Pydantic v1 fallback


def _safe_http_mapping(value: Any) -> dict[str, Any]:
    """Apply the evidence redaction boundary to repository-owned values."""

    from .observability import sanitize_mapping

    safe = sanitize_mapping(_as_mapping(value), max_length=2_000)
    return dict(safe) if isinstance(safe, Mapping) else {"value": safe}


def _invoke_runtime_factory(factory: Callable[..., Any], task: Any) -> Any:
    """Call a factory once using its declared signature.

    Retrying after an arbitrary ``TypeError`` can execute a side-effecting
    factory twice and hide a real bug.  Signature inspection distinguishes a
    no-argument factory from a factory whose implementation itself failed.
    """

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory(task)
    parameters = list(signature.parameters.values())
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return factory(task=task)
    task_parameter = signature.parameters.get("task")
    if task_parameter is not None and task_parameter.kind == inspect.Parameter.KEYWORD_ONLY:
        return factory(task=task)
    if any(
        parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    ) or any(parameter.kind == inspect.Parameter.VAR_POSITIONAL for parameter in parameters):
        return factory(task)
    return factory()


async def _await_if_needed(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await cast(Awaitable[Any], value)
    return value


async def _execute_task(
    request: TaskRequest,
    *,
    runtime: Any | None,
    runtime_factory: Callable[..., Any] | None,
) -> Any:
    task = _to_runtime_task(request)
    target = runtime
    if target is None and runtime_factory is not None:
        target = await _await_if_needed(_invoke_runtime_factory(runtime_factory, task))
    if target is None:
        raise RuntimeError("No Agent Runtime is configured")
    # A lightweight factory used by tests or a queue adapter may execute the
    # task itself and return the result mapping rather than a runtime object.
    if isinstance(target, Mapping):
        return target
    method = target if callable(target) and not hasattr(target, "run") else None
    if method is None:
        for name in ("run", "run_task", "execute"):
            candidate = getattr(target, name, None)
            if callable(candidate):
                method = candidate
                break
    if method is None:
        raise RuntimeError("Configured Agent Runtime has no run method")
    return await _await_if_needed(method(task))


def _default_store() -> Any:
    try:
        from .paths import config_dir
        from .storage import SQLiteRepository  # type: ignore[import-not-found]

        return SQLiteRepository(Path(config_dir()) / "tasklens.db")
    except Exception as exc:  # noqa: BLE001 - startup must degrade without data loss claims
        from .observability import sanitize_text

        return _MemoryStore(reason=sanitize_text(f"SQLite unavailable: {exc}", 240))


def _dashboard_html() -> str:
    # The page is static; runtime data is always inserted with textContent in
    # the browser to avoid turning a task/URL field into executable HTML.
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TaskLens · Run Observatory</title>
  <style>
    :root { color-scheme: light; --ink:#172033; --muted:#64748b; --line:#dbe3ef; --panel:#fff; --accent:#3858d6; --danger:#c24155; --ok:#147d5a; }
    * { box-sizing:border-box; }
    body { margin:0; background:#f4f7fb; color:var(--ink); font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
    main { max-width:1120px; margin:0 auto; padding:32px 20px 56px; }
    header { display:flex; align-items:end; justify-content:space-between; gap:16px; margin-bottom:24px; }
    h1 { margin:0; font-size:30px; letter-spacing:-.03em; } h2 { margin:0 0 12px; font-size:18px; }
    .subtitle { margin:4px 0 0; color:var(--muted); } .muted { color:var(--muted); }
    .grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:22px; }
    .panel,.metric { background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:0 5px 20px #233b6810; }
    .metric { padding:16px; } .metric label { color:var(--muted); display:block; font-size:12px; text-transform:uppercase; letter-spacing:.08em; }
    .metric strong { display:block; font-size:26px; margin-top:4px; }
    .panel { padding:18px; } .toolbar { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
    select,button { border:1px solid var(--line); background:#fff; color:var(--ink); border-radius:8px; padding:8px 10px; }
    button { cursor:pointer; } button:hover { border-color:var(--accent); color:var(--accent); }
    table { width:100%; border-collapse:collapse; } th,td { text-align:left; border-bottom:1px solid var(--line); padding:10px 8px; vertical-align:top; }
    th { color:var(--muted); font-size:12px; font-weight:600; } tbody tr { cursor:pointer; } tbody tr:hover { background:#f6f8ff; }
    .pill { display:inline-block; border-radius:999px; padding:2px 8px; font-size:12px; background:#edf2ff; color:var(--accent); }
    .pill.failed { background:#fff0f1; color:var(--danger); } .pill.success { background:#eaf8f2; color:var(--ok); }
    #detail { margin-top:18px; } .detail-meta { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; margin:0 0 14px; } .detail-meta div { background:#f7f9fd; border-radius:9px; padding:9px; } .detail-meta small { color:var(--muted); display:block; } .step { border-left:3px solid var(--line); padding:8px 12px; margin:8px 0; } .step.failed { border-color:var(--danger); } .assertion { margin:6px 0 0 14px; color:var(--muted); font-size:13px; }
    .empty { padding:28px; text-align:center; color:var(--muted); } .error { color:var(--danger); }
    @media (max-width:700px) { main { padding:22px 12px 36px; } header { display:block; } .grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .panel { padding:12px; } .detail-meta { grid-template-columns:1fr 1fr; } table,thead,tbody,tr,td { display:block; width:100%; } thead { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; } tbody tr { border:1px solid var(--line); border-radius:10px; margin:8px 0; padding:4px 8px; } td { display:grid; grid-template-columns:86px minmax(0,1fr); gap:8px; border:0; padding:6px 0; overflow-wrap:anywhere; } td::before { content:attr(data-label); color:var(--muted); font-size:12px; font-weight:600; } }
  </style>
</head>
<body>
<main>
  <header><div><h1>TaskLens</h1><p class="subtitle">Single-agent browser test evidence</p></div><button id="refresh" type="button">Refresh</button></header>
  <section class="grid" aria-label="Run summary">
    <div class="metric"><label>Total runs</label><strong id="total">—</strong></div>
    <div class="metric"><label>Succeeded</label><strong id="succeeded">—</strong></div>
    <div class="metric"><label>Failed</label><strong id="failed">—</strong></div>
    <div class="metric"><label>Avg duration</label><strong id="average">—</strong></div>
  </section>
  <section class="panel"><h2>Recent runs</h2><div class="toolbar"><label class="muted" for="status">Status</label><select id="status"><option value="">All</option><option>success</option><option>failed</option><option>timeout</option><option>cancelled</option></select></div><div id="list" class="empty">Loading runs…</div></section>
  <section id="detail" class="panel" hidden><h2>Run detail</h2><div id="detail-body"></div></section>
</main>
<script>
(() => {
  const $ = (id) => document.getElementById(id);
  const text = (node, value) => { node.textContent = value == null ? '—' : String(value); };
  const pill = (value) => { const node = document.createElement('span'); node.className = 'pill ' + (value || ''); node.textContent = value || 'unknown'; return node; };
  async function read(url) { const response = await fetch(url); const body = await response.json(); if (!response.ok || !body.success) throw new Error(body.error?.message || 'Request failed'); return body.data; }
  async function load() {
    try {
      const [summary, runs] = await Promise.all([read('/api/summary'), read('/api/runs?limit=50' + ($('status').value ? '&status=' + encodeURIComponent($('status').value) : ''))]);
      text($('total'), summary.total ?? summary.total_runs); text($('succeeded'), summary.succeeded ?? summary.success_count); text($('failed'), summary.failed ?? summary.failed_count); text($('average'), summary.average_duration_seconds == null ? '—' : Number(summary.average_duration_seconds).toFixed(2) + 's');
      renderRuns(runs || []);
    } catch (error) { $('list').className = 'empty error'; text($('list'), error.message || 'Unable to load runs'); }
  }
  function renderRuns(runs) {
    const host = $('list'); host.replaceChildren(); if (!runs.length) { host.className = 'empty'; text(host, 'No recorded runs yet.'); return; }
    host.className = '';
    const table = document.createElement('table'); const head = document.createElement('thead'); const row = document.createElement('tr'); ['Status','Task','Browser','Duration','Started','Run ID'].forEach((label) => { const cell = document.createElement('th'); text(cell,label); row.append(cell); }); head.append(row); table.append(head);
    const body = document.createElement('tbody'); runs.forEach((run) => { const tr = document.createElement('tr'); tr.tabIndex = 0; tr.setAttribute('role', 'button'); const open = () => showDetail(run.run_id); tr.addEventListener('click', open); tr.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); open(); } }); const cell = (label, value) => { const node = document.createElement('td'); node.dataset.label = label; if (value && value.nodeType) node.append(value); else text(node, value); return node; }; const status = document.createElement('td'); status.dataset.label = 'Status'; status.append(pill(run.status)); const started = run.started_at ? new Date(run.started_at).toLocaleString() : '—'; tr.append(status, cell('Task', run.task_name || run.goal || 'Untitled task'), cell('Browser', run.browser_backend || 'unknown'), cell('Duration', run.duration_seconds == null ? '—' : Number(run.duration_seconds).toFixed(2) + 's'), cell('Started', started), cell('Run ID', run.run_id)); body.append(tr); }); table.append(body); host.append(table);
  }
  async function showDetail(runId) {
    const section = $('detail'); const host = $('detail-body'); section.hidden = false; host.replaceChildren(); text(host, 'Loading…');
    try { const run = await read('/api/runs/' + encodeURIComponent(runId)); host.replaceChildren(); const meta = document.createElement('div'); meta.className = 'detail-meta'; const addMeta = (label, value) => { const item = document.createElement('div'); const caption = document.createElement('small'); text(caption, label); const content = document.createElement('span'); text(content, value); item.append(caption, content); meta.append(item); }; addMeta('Status', run.status || 'unknown'); addMeta('Failure', run.failure_kind || 'none'); addMeta('First deviation', run.first_deviation_step == null ? '—' : '#' + run.first_deviation_step); addMeta('Exit code', run.exit_code == null ? '—' : run.exit_code); addMeta('Started', run.started_at ? new Date(run.started_at).toLocaleString() : '—'); addMeta('Duration', run.duration_seconds == null ? '—' : Number(run.duration_seconds).toFixed(2) + 's'); host.append(meta); const intro = document.createElement('p'); text(intro, run.goal || run.task_name || 'Run'); host.append(intro); if (run.error_summary) { const error = document.createElement('p'); error.className = 'error'; text(error, run.error_summary); host.append(error); } (run.steps || []).slice().sort((a,b) => Number(b.duration_seconds||0)-Number(a.duration_seconds||0)).forEach((step) => { const item = document.createElement('div'); item.className = 'step ' + (step.status === 'failed' ? 'failed' : ''); text(item, '#' + (step.sequence || '') + ' ' + (step.helper || step.action || 'step') + ' · ' + (step.duration_seconds == null ? '—' : Number(step.duration_seconds).toFixed(2) + 's') + (step.error_summary ? ' · ' + step.error_summary : '')); (step.assertions || []).forEach((assertion) => { const line = document.createElement('div'); line.className = 'assertion'; text(line, (assertion.status || 'unknown') + ' · ' + (assertion.name || assertion.type || 'assertion') + (assertion.evidence ? ' · ' + assertion.evidence : '')); item.append(line); }); item.dataset.sequence = String(step.sequence || ''); host.append(item); }); } catch (error) { host.replaceChildren(); const message = document.createElement('p'); message.className = 'error'; text(message, error.message || 'Unable to load detail'); host.append(message); }
  }
  $('refresh').addEventListener('click', load); $('status').addEventListener('change', load); load();
})();
</script>
</body></html>"""


def create_app(
    *,
    store: StoreProtocol | Any | None = None,
    runtime: RuntimeProtocol | Any | None = None,
    runtime_factory: Callable[..., Any] | None = None,
) -> FastAPI:
    """Create the TaskLens FastAPI application.

    ``store`` and ``runtime_factory`` are explicit injection points for tests,
    Docker deployments and future CLI wiring.  No network listener is started
    by this function.
    """

    repository = store if store is not None else _default_store()
    app = FastAPI(title="TaskLens", docs_url="/api/docs", redoc_url=None)
    app.state.store = repository
    app.state.runtime = runtime
    if runtime_factory is None and runtime is None:
        # Resolve model/browser configuration only when a task is submitted;
        # importing the dashboard must remain side-effect free.
        try:
            from .tasklens_runtime import runtime_from_environment

            runtime_factory = runtime_from_environment
        except ImportError:
            runtime_factory = None
    app.state.runtime_factory = runtime_factory

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if any(str(item.get("type", "")).startswith("json_invalid") for item in exc.errors()):
            return _error_response(400, "invalid_json", "Request body must be valid JSON")
        return _error_response(
            400,
            "validation_error",
            "Request validation failed",
            details=_validation_details(exc),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code_by_status = {404: "not_found", 405: "method_not_allowed"}
        code = code_by_status.get(exc.status_code, "http_error")
        detail = exc.detail if isinstance(exc.detail, str) else "HTTP request failed"
        return _error_response(exc.status_code, code, detail)

    @app.exception_handler(json.JSONDecodeError)
    async def json_decode_handler(_request: Request, _exc: json.JSONDecodeError) -> JSONResponse:
        return _error_response(400, "invalid_json", "Request body must be valid JSON")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_dashboard_html())

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # Avoid a noisy browser-console 404 during the otherwise self-contained
        # dashboard smoke test; a custom icon is intentionally out of P0 scope.
        return Response(status_code=204)

    @app.get("/api/health")
    async def health() -> JSONResponse:
        try:
            data = dict(_invoke_store_health(repository))
            data.setdefault("status", "ok")
            return JSONResponse(_envelope(success=True, data=data))
        except Exception as exc:  # noqa: BLE001 - health must remain non-throwing
            return JSONResponse(
                _envelope(
                    success=True,
                    data={"status": "degraded", "storage": "unavailable", "error": type(exc).__name__},
                )
            )

    @app.get("/api/summary")
    async def summary() -> JSONResponse:
        try:
            data = _normalize_summary(_invoke_store_summary(repository))
            return JSONResponse(_envelope(success=True, data=data))
        except Exception:  # noqa: BLE001 - repository implementations are injected
            return _error_response(503, "storage_unavailable", "Run summary is temporarily unavailable")

    @app.get("/api/runs")
    async def runs(
        limit: int = DEFAULT_RUN_LIMIT,
        status: str | None = None,
        browser: str | None = None,
    ) -> JSONResponse:
        if limit < 1 or limit > MAX_RUN_LIMIT:
            return _error_response(400, "validation_error", "limit must be between 1 and 100")
        if status is not None and status not in VALID_STATUSES:
            return _error_response(400, "validation_error", "status is not a supported final status")
        if browser is not None and browser not in VALID_BROWSERS:
            return _error_response(400, "validation_error", "browser is not a supported backend")
        try:
            data = [
                _safe_http_mapping(item)
                for item in _invoke_store_list(repository, limit=limit, status=status, browser=browser)
            ]
            return JSONResponse(_envelope(success=True, data=data, meta={"limit": limit}))
        except Exception:  # noqa: BLE001 - repository implementations are injected
            return _error_response(503, "storage_unavailable", "Run history is temporarily unavailable")

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> JSONResponse:
        if not run_id.strip() or len(run_id) > 128:
            return _error_response(400, "validation_error", "run_id is invalid")
        try:
            result = _invoke_store_detail(repository, run_id)
        except Exception:  # noqa: BLE001 - repository implementations are injected
            return _error_response(503, "storage_unavailable", "Run detail is temporarily unavailable")
        if result is None:
            return _error_response(404, "not_found", f"Run '{run_id}' was not found")
        return JSONResponse(_envelope(success=True, data=_safe_http_mapping(result)))

    @app.post("/api/tasks", status_code=202)
    async def submit_task(payload: TaskRequest) -> JSONResponse:
        try:
            result = await _execute_task(
                payload,
                runtime=app.state.runtime,
                runtime_factory=app.state.runtime_factory,
            )
        except ValidationError as exc:
            return _error_response(400, "validation_error", "Task validation failed", details=_validation_details(exc))
        except RuntimeError as exc:
            return _error_response(503, "runtime_unavailable", str(exc))
        except Exception as exc:  # noqa: BLE001 - runtime boundary returns stable API errors
            if getattr(exc, "failure_kind", None) in {
                "environment",
                "connection",
                "timeout",
            }:
                return _error_response(503, "runtime_unavailable", "Task runtime is unavailable")
            return _error_response(500, "runtime_error", "Task execution failed")

        data = _as_mapping(result)
        if not data.get("run_id"):
            data["run_id"] = str(uuid4())

        # Runtime/provider output is untrusted page-derived data.  Apply the
        # same recursive redaction and URL normalization used by SQLite before
        # returning it over HTTP; the dashboard must not become a credential
        # leak just because a provider returned an unexpected field.
        try:
            from .observability import sanitize_mapping

            safe_data = sanitize_mapping(data, max_length=2_000)
            data = safe_data if isinstance(safe_data, dict) else {"value": safe_data}
        except (TypeError, ValueError):
            data = {"run_id": str(data.get("run_id", uuid4())), "status": data.get("status", "unknown")}

        # Reuse the CLI's narrow result-to-evidence bridge so API and CLI
        # submissions produce the same SQLite RunRecord/StepRecord shape.
        # Persistence is deliberately best-effort: a storage outage must not
        # rewrite the Agent Runtime's primary result.
        persisted = False
        warning: str | None = None
        try:
            from .tasklens_cli import persist_result

            task = _to_runtime_task(payload)
            persisted, warning = persist_result(repository, data, task)
        except Exception:  # noqa: BLE001 - the observation boundary is fail-open
            warning = "persistence_failed"
        meta: dict[str, Any] = {"persisted": persisted}
        if warning:
            meta["storage_warning"] = warning
        return JSONResponse(_envelope(success=True, data=data, meta=meta), status_code=202)

    return app


app = create_app()

__all__ = [
    "AssertionSpec",
    "RetryPolicy",
    "StoreProtocol",
    "TaskConstraints",
    "TaskRequest",
    "app",
    "create_app",
]
