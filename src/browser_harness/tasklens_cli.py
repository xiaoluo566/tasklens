"""Independent TaskLens command-line entry point.

The original :mod:`browser_harness.run` command executes arbitrary helper
scripts and is intentionally left untouched.  This module is the small,
structured TaskLens surface around the new single-agent runtime:

* ``task`` builds a validated ``TaskSpec`` from JSON and/or explicit flags;
* an injected runtime is invoked through a narrow, async-friendly protocol;
* an injected repository receives a bounded result on a best-effort basis;
* every normal and error path emits one JSON envelope and a stable exit code;
* ``dashboard`` creates the existing FastAPI app without recording itself.

No browser, model provider, or database is created at import time.  That is
important both for the legacy harness CLI and for deterministic unit tests.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import sys
import webbrowser
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn, TextIO, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from .model_provider import ProviderError
from .observability import sanitize_mapping, sanitize_text
from .tasklens_domain import TaskSpec


class CliError(ValueError):
    """An expected CLI/input error rendered as a structured envelope."""

    def __init__(self, code: str, message: str, *, details: Any = None, exit_code: int = 2) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.exit_code = exit_code


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that does not print an unstructured usage banner."""

    def error(self, message: str) -> NoReturn:  # pragma: no cover - exercised through main
        raise CliError("usage_error", message)


class _HelpAction(argparse.Action):
    """Raise a structured success response instead of printing raw help."""

    def __call__(self, parser: argparse.ArgumentParser, *_: Any, **__: Any) -> None:
        raise CliError("help", parser.format_help(), exit_code=0)


def _jsonable(value: Any) -> Any:
    """Convert an injected result to JSON without depending on its class."""

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


def _safe_message(value: Any, limit: int = 500) -> str:
    """Redact and bound a boundary error before it reaches the terminal/API."""

    return sanitize_text(value, limit) or "unknown error"


def _load_json_object(raw: str, *, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CliError("validation_error", f"{source} must contain valid JSON") from exc
    if not isinstance(value, Mapping):
        raise CliError("validation_error", f"{source} must contain a JSON object")
    return {str(key): item for key, item in value.items()}


def _load_task_payload(
    *,
    task_json: str | None,
    task_file: str | Path | None,
    read_stdin: bool = False,
    stdin: TextIO | None = None,
) -> dict[str, Any]:
    if read_stdin and (task_json is not None or task_file is not None):
        raise CliError("usage_error", "--stdin cannot be combined with --task-json or --task-file")
    if read_stdin:
        return _load_json_object((stdin or sys.stdin).read(), source="stdin")
    if task_json is not None and task_file is not None:
        raise CliError("usage_error", "--task-json and --task-file are mutually exclusive")
    if task_file is not None:
        path = Path(task_file)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError("validation_error", f"unable to read task file: {path}") from exc
        return _load_json_object(raw, source="task file")
    if task_json is None:
        return {}
    if task_json == "-":
        return _load_json_object(sys.stdin.read(), source="stdin")
    return _load_json_object(task_json, source="--task-json")


def _parse_json_flag(raw: str, *, name: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise CliError("validation_error", f"{name} must contain valid JSON") from exc


def _parse_assertion(raw: str) -> Any:
    """Accept a JSON assertion or a convenient text-contains shorthand."""

    stripped = raw.strip()
    if not stripped:
        raise CliError("validation_error", "--assertion must not be blank")
    if stripped.startswith("{"):
        value = _parse_json_flag(stripped, name="--assertion")
        if not isinstance(value, Mapping):
            raise CliError("validation_error", "--assertion JSON must be an object")
        return dict(value)
    return {"type": "text", "expected": raw, "operator": "contains"}


def build_task_spec(
    payload: Mapping[str, Any] | None = None,
    *,
    goal: str | None = None,
    task_id: str | None = None,
    max_steps: int | None = None,
    timeout_seconds: float | None = None,
    assertions: Sequence[Any] | None = None,
    retry_policy: Mapping[str, Any] | None = None,
) -> TaskSpec:
    """Merge CLI values into a validated :class:`TaskSpec`.

    Explicit command-line values take precedence over the JSON object.  The
    function is public so API adapters and tests can construct exactly the
    same contract as the CLI without spawning a subprocess.
    """

    values = dict(payload or {})
    if goal is not None:
        values["goal"] = goal
    if task_id is not None:
        values["task_id"] = task_id
    if max_steps is not None:
        values["max_steps"] = max_steps
    if timeout_seconds is not None:
        values["timeout_seconds"] = timeout_seconds
    if assertions:
        existing = values.get("assertions") or []
        if not isinstance(existing, Sequence) or isinstance(existing, (str, bytes, bytearray)):
            raise CliError("validation_error", "task assertions must be a JSON array")
        values["assertions"] = [*existing, *assertions]
    if retry_policy is not None:
        values["retry_policy"] = dict(retry_policy)
    try:
        if hasattr(TaskSpec, "model_validate"):
            return TaskSpec.model_validate(values)
        return TaskSpec(**values)  # pragma: no cover - Pydantic v1 fallback
    except (ValidationError, TypeError, ValueError) as exc:
        details = exc.errors() if isinstance(exc, ValidationError) else None
        raise CliError(
            "validation_error",
            "task specification is invalid",
            details=details,
        ) from exc


async def _await_if_needed(value: Any) -> Any:
    if isinstance(value, Awaitable):
        return await value
    return value


async def invoke_runtime(runtime: Any, task: TaskSpec) -> Any:
    """Invoke an injected runtime using the smallest compatible protocol."""

    if runtime is None:
        raise CliError(
            "runtime_unavailable",
            "no Agent Runtime was injected; configure a runtime before running a task",
            exit_code=1,
        )
    target: Any = runtime
    if not callable(target) or hasattr(target, "run"):
        for name in ("run", "run_task", "execute"):
            method = getattr(target, name, None)
            if callable(method):
                target = method
                break
    if not callable(target):
        raise CliError("runtime_unavailable", "configured runtime has no run method", exit_code=1)
    try:
        return await _await_if_needed(target(task))
    except CliError:
        raise
    except Exception as exc:
        raise CliError("runtime_error", _safe_message(str(exc) or type(exc).__name__), exit_code=1) from exc


def _result_status(result: Mapping[str, Any]) -> tuple[bool, str]:
    status = str(result.get("status", "failed")).strip().lower()
    default_failure = "none" if status == "success" else "unknown"
    failure_kind = str(result.get("failure_kind", default_failure)).strip().lower()
    succeeded = status == "success" and failure_kind in {"", "none", "success"}
    return succeeded, status


def _normalise_result(result: Any, task: TaskSpec) -> dict[str, Any]:
    value = _jsonable(result)
    mapping_value = sanitize_mapping(value, max_length=2_000)
    mapping = dict(mapping_value) if isinstance(mapping_value, Mapping) else {"value": mapping_value}
    mapping.setdefault("run_id", str(uuid4()))
    status = str(mapping.get("status", "")).strip().lower()
    if not status and isinstance(mapping.get("success"), bool):
        mapping["status"] = "success" if mapping["success"] else "failed"
        status = str(mapping["status"])
    if not status:
        mapping["status"] = "failed"
    if "failure_kind" not in mapping:
        mapping["failure_kind"] = "none" if mapping["status"] == "success" else "unknown"
    mapping.setdefault("task_id", task.task_id)
    return mapping


def _result_to_run_record(result: Mapping[str, Any], task: TaskSpec) -> dict[str, Any]:
    """Adapt an AgentResult mapping to the storage repository's run contract."""

    succeeded, _ = _result_status(result)
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(result.get("steps") or [], start=1):
        step = _jsonable(raw_step)
        if not isinstance(step, Mapping):
            step = {"error_summary": str(step)}
        action = step.get("action")
        action_map = action if isinstance(action, Mapping) else {}
        outcome = step.get("outcome")
        outcome_map = outcome if isinstance(outcome, Mapping) else {}
        observation = step.get("observation")
        observation_map = observation if isinstance(observation, Mapping) else {}
        assertions = []
        for assertion in step.get("assertions") or []:
            item = assertion if isinstance(assertion, Mapping) else {}
            assertions.append(
                {
                    "name": item.get("name", "assertion"),
                    "type": item.get("assertion_type", item.get("type", "text")),
                    "status": item.get("status", "passed" if item.get("passed") else "failed"),
                    "operator": item.get("operator"),
                    "expected": item.get("expected"),
                    "actual": item.get("actual"),
                    "evidence": item.get("evidence"),
                    "error_summary": item.get("error"),
                }
            )
        steps.append(
            {
                "sequence": step.get("sequence", index),
                "helper": step.get("helper") or action_map.get("kind", "unknown"),
                "observation_summary": observation_map.get("text", ""),
                "action": action_map.get("kind", ""),
                "args_summary": action_map.get("args", {}),
                "duration_seconds": step.get("duration_seconds", 0),
                "status": step.get("status", "success"),
                "failure_kind": step.get("failure_kind", outcome_map.get("failure_kind", "none")),
                "error_summary": step.get("error") or outcome_map.get("error"),
                "assertions": assertions,
            }
        )
    started_at = result.get("started_at")
    if started_at is None:
        started_at = datetime.now(UTC).isoformat()
    record = {
        "run_id": str(result.get("run_id") or task.task_id),
        "task_name": task.task_id,
        "goal": task.goal,
        "task_preview": task.goal,
        "task_length": len(task.goal),
        "started_at": started_at,
        "duration_seconds": result.get("duration_seconds", 0),
        "status": result.get("status", "failed"),
        "failure_kind": result.get("failure_kind", "unknown"),
        "first_deviation_step": result.get("first_deviation_step"),
        "browser_backend": result.get("browser_backend", "unknown"),
        "exit_code": 0 if succeeded else 1,
        "output_tail": result.get("answer_summary", ""),
        "answer_summary": result.get("answer_summary", ""),
        "max_steps": task.max_steps,
        "steps": steps,
    }
    if result.get("finished_at") is not None:
        record["finished_at"] = result["finished_at"]
    return record


def persist_result(store: Any, result: Any, task: TaskSpec) -> tuple[bool, str | None]:
    """Best-effort persistence; never changes the primary task result."""

    if store is None:
        return False, None
    result_mapping = sanitize_mapping(_jsonable(result), max_length=2_000)
    if not isinstance(result_mapping, Mapping):
        result_mapping = {"value": result_mapping}
    try:
        append_result = getattr(store, "append_result", None)
        if callable(append_result):
            value = append_result(dict(result_mapping))
            return value is not False, None
        append_safe = getattr(store, "append_run_safe", None)
        if callable(append_safe):
            value = append_safe(_result_to_run_record(result_mapping, task))
            return bool(value), None if value else "persistence_failed"
        append_run = getattr(store, "append_run", None)
        if callable(append_run):
            append_run(_result_to_run_record(result_mapping, task))
            return True, None
        return False, "store_has_no_append_method"
    except Exception:  # noqa: BLE001 - documented fail-open boundary
        return False, "persistence_failed"


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="tasklens", add_help=False)
    parser.add_argument("-h", "--help", nargs=0, action=_HelpAction)
    sub = parser.add_subparsers(dest="command")

    task = sub.add_parser("task", add_help=False)
    task.add_argument("-h", "--help", nargs=0, action=_HelpAction)
    task.add_argument("--goal")
    task.add_argument("--task-id")
    task.add_argument("--task-json", "--task")
    task.add_argument("--task-file")
    task.add_argument("--stdin", action="store_true")
    task.add_argument("--max-steps", type=int)
    task.add_argument("--timeout-seconds", type=float)
    task.add_argument("--assertion", action="append", default=[])
    task.add_argument("--retry-policy")
    task.add_argument("--demo", action="store_true")

    dashboard = sub.add_parser("dashboard", add_help=False)
    dashboard.add_argument("-h", "--help", nargs=0, action=_HelpAction)
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--open", action="store_true")
    dashboard.add_argument(
        "--allow-remote",
        action="store_true",
        help="explicitly allow a non-loopback host (the dashboard has no auth/rate limiting)",
    )
    dashboard.add_argument("--demo", action="store_true")
    return parser


def _write(output: TextIO, value: Mapping[str, Any]) -> None:
    output.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()


def _invoke_dashboard_runner(
    runner: Callable[..., Any],
    app: Any,
    *,
    host: str,
    port: int,
) -> Any:
    """Call an injected server runner once using its declared signature.

    Test and embedding runners sometimes expose positional-only host/port
    parameters, while ``uvicorn.run`` accepts keywords.  Inspecting the
    signature before invocation preserves that compatibility without catching
    a ``TypeError`` raised *inside* the runner and accidentally executing it a
    second time.
    """

    try:
        signature = inspect.signature(runner)
    except (TypeError, ValueError):
        return runner(app, host=host, port=port)
    try:
        signature.bind(app, host=host, port=port)
    except TypeError:
        try:
            signature.bind(app, host, port)
        except TypeError:
            # Let the actual call produce the most useful boundary error; it
            # will be converted to the structured dashboard error by caller.
            return runner(app, host=host, port=port)
        return runner(app, host, port)
    return runner(app, host=host, port=port)


def _run_dashboard(
    args: argparse.Namespace,
    *,
    store: Any,
    uvicorn_runner: Callable[..., Any] | None,
    browser_opener: Callable[[str], Any] | None,
    stderr: TextIO | None = None,
) -> int:
    if not 1 <= args.port <= 65535:
        raise CliError("usage_error", "port must be between 1 and 65535")
    if not args.host.strip():
        raise CliError("usage_error", "host must not be blank")
    if not _is_loopback_host(args.host) and not bool(getattr(args, "allow_remote", False)):
        raise CliError(
            "unsafe_host",
            "non-loopback dashboard hosts require --allow-remote; use SSH tunneling or add authentication first",
        )
    if not _is_loopback_host(args.host) and stderr is not None:
        stderr.write("WARNING: remote Dashboard has no authentication or rate limiting; restrict access.\n")
    try:
        from .dashboard import create_app

        runtime_for_app = None
        if args.demo:
            from .tasklens_runtime import create_runtime

            runtime_for_app = create_runtime(
                environ={"TASKLENS_MODEL_PROVIDER": "demo"}
            )
        app = create_app(store=store, runtime=runtime_for_app)
    except Exception as exc:  # pragma: no cover - dependency/environment path
        raise CliError("dashboard_error", _safe_message(exc), exit_code=1) from exc
    url = f"http://{args.host}:{args.port}"
    if stderr is not None:
        stderr.write(f"TaskLens Dashboard: {url}\n")
    if args.open:
        opener = browser_opener or webbrowser.open
        try:
            opener(url)
        except Exception as exc:
            raise CliError("dashboard_error", f"unable to open browser: {_safe_message(exc)}", exit_code=1) from exc
    runner: Callable[..., Any] | None = uvicorn_runner
    if runner is None:
        try:
            import uvicorn

            runner = cast(Callable[..., Any], uvicorn.run)
        except ImportError as exc:  # pragma: no cover - dependency is project optional
            raise CliError("dashboard_error", "uvicorn is not installed", exit_code=1) from exc
    runner_fn = cast(Callable[..., Any], runner)
    try:
        _invoke_dashboard_runner(runner_fn, app, host=args.host, port=args.port)
    except Exception as exc:
        raise CliError("dashboard_error", _safe_message(exc), exit_code=1) from exc
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime: Any | None = None,
    store: Any | None = None,
    uvicorn_runner: Callable[..., Any] | None = None,
    browser_opener: Callable[[str], Any] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Run the independent TaskLens CLI and return a process exit code."""

    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
        if args.command == "dashboard":
            try:
                if not 1 <= args.port <= 65535:
                    raise CliError("usage_error", "port must be between 1 and 65535")
                if not args.host.strip():
                    raise CliError("usage_error", "host must not be blank")
                code = _run_dashboard(
                    args,
                    store=store,
                    uvicorn_runner=uvicorn_runner,
                    browser_opener=browser_opener,
                    stderr=err,
                )
            except CliError as exc:
                details = {"code": exc.code, "message": exc.message}
                if exc.details is not None:
                    details["details"] = exc.details
                _write(out, _envelope(success=False, error=details))
                if exc.message and exc.code != "help":
                    err.write(exc.message + "\n")
                return exc.exit_code
            _write(out, _envelope(success=True, data={"url": f"http://{args.host}:{args.port}"}))
            return code
        if args.command != "task":
            raise CliError("usage_error", "a command is required: task or dashboard")

        payload = _load_task_payload(
            task_json=args.task_json,
            task_file=args.task_file,
            read_stdin=args.stdin,
            stdin=stdin,
        )
        assertions = [_parse_assertion(item) for item in args.assertion]
        retry_policy = (
            _parse_json_flag(args.retry_policy, name="--retry-policy")
            if args.retry_policy is not None
            else None
        )
        if retry_policy is not None and not isinstance(retry_policy, Mapping):
            raise CliError("validation_error", "--retry-policy JSON must be an object")
        task = build_task_spec(
            payload,
            goal=args.goal,
            task_id=args.task_id,
            max_steps=args.max_steps,
            timeout_seconds=args.timeout_seconds,
            assertions=assertions,
            retry_policy=retry_policy,
        )
        selected_runtime = runtime
        if selected_runtime is None:
            try:
                from .tasklens_runtime import create_runtime

                selected_runtime = create_runtime(
                    environ={"TASKLENS_MODEL_PROVIDER": "demo"} if args.demo else None
                )
            except (ImportError, OSError, TypeError, ValueError, ProviderError) as exc:
                raise CliError(
                    "runtime_unavailable",
                    _safe_message(str(exc)),
                    exit_code=1,
                ) from exc
        result = asyncio.run(invoke_runtime(selected_runtime, task))
        result_mapping = _normalise_result(result, task)
        succeeded, status = _result_status(result_mapping)
        persistence_store = store
        if persistence_store is None:
            try:
                from .storage import SQLiteRepository

                persistence_store = SQLiteRepository()
            except (ImportError, OSError, TypeError, ValueError):
                persistence_store = None
        persisted, warning = persist_result(persistence_store, result_mapping, task)
        meta: dict[str, Any] = {"persisted": persisted}
        if warning:
            meta["storage_warning"] = warning
        if succeeded:
            _write(out, _envelope(success=True, data=result_mapping, meta=meta))
            return 0
        message = str(
            result_mapping.get("error_summary")
            or result_mapping.get("answer_summary")
            or f"task finished with status: {status}"
        )
        _write(
            out,
            _envelope(
                success=False,
                data=result_mapping,
                error={"code": "task_failed", "message": message},
                meta=meta,
            ),
        )
        return 1
    except CliError as exc:
        if exc.code == "help":
            _write(out, _envelope(success=True, data={"help": exc.message}))
            return exc.exit_code
        details: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.details is not None:
            details["details"] = exc.details
        _write(out, _envelope(success=False, error=details))
        if exc.message:
            err.write(exc.message + "\n")
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - final structured safety net
        message = _safe_message(str(exc) or type(exc).__name__)
        _write(out, _envelope(success=False, error={"code": "cli_error", "message": message}))
        err.write(message + "\n")
        return 1


__all__ = [
    "CliError",
    "build_task_spec",
    "invoke_runtime",
    "main",
    "persist_result",
]


def _is_loopback_host(host: str) -> bool:
    return host.strip().lower().strip("[]") in {"127.0.0.1", "localhost", "::1"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
