"""Contract tests for the independent TaskLens command-line entry point.

The CLI tests inject a deterministic runtime and an in-memory store.  They do
not start Chrome, CDP, Uvicorn, or an external model provider.  This keeps the
command contract testable while the browser adapter and runtime evolve.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from browser_harness.tasklens_cli import main


@dataclass
class FakeRuntime:
    result: Any
    tasks: list[Any] = field(default_factory=list)

    async def run(self, task: Any) -> Any:
        self.tasks.append(task)
        return self.result


@dataclass
class FakeStore:
    appended: list[Any] = field(default_factory=list)

    def append_result(self, result: Any) -> str:
        self.appended.append(result)
        return str(result.get("run_id", "run-created"))


def _invoke(
    argv: list[str],
    *,
    runtime: Any | None = None,
    store: Any | None = None,
    uvicorn_runner: Any | None = None,
    browser_opener: Any | None = None,
) -> tuple[int, dict[str, Any], str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        argv,
        runtime=runtime,
        store=store,
        uvicorn_runner=uvicorn_runner,
        browser_opener=browser_opener,
        stdout=stdout,
        stderr=stderr,
    )
    body = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {}
    return code, body, stderr.getvalue()


def test_task_goal_builds_valid_task_spec_and_returns_run_id() -> None:
    runtime = FakeRuntime(
        {"run_id": "run-1", "status": "success", "failure_kind": "none"}
    )
    store = FakeStore()

    code, body, _ = _invoke(
        [
            "task",
            "--goal",
            "check the order",
            "--task-id",
            "checkout-smoke",
            "--max-steps",
            "4",
            "--timeout-seconds",
            "7",
        ],
        runtime=runtime,
        store=store,
    )

    assert code == 0
    assert body["success"] is True
    assert body["data"]["run_id"] == "run-1"
    assert len(runtime.tasks) == 1
    task = runtime.tasks[0]
    assert task.goal == "check the order"
    assert task.task_id == "checkout-smoke"
    assert task.max_steps == 4
    assert task.timeout_seconds == 7
    assert store.appended[0]["run_id"] == "run-1"


def test_task_json_file_accepts_nested_constraints_and_assertion_override(
    tmp_path: Path,
) -> None:
    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task_id": "order-check",
                "goal": "verify order status",
                "constraints": {"max_steps": 9, "timeout_seconds": 12},
                "assertions": [],
            }
        ),
        encoding="utf-8",
    )
    runtime = FakeRuntime(
        {"run_id": "run-json", "status": "success", "failure_kind": "none"}
    )

    code, body, _ = _invoke(
        [
            "task",
            "--task-file",
            str(task_file),
            "--assertion",
            '{"type":"text","expected":"Submitted","operator":"contains"}',
        ],
        runtime=runtime,
        store=FakeStore(),
    )

    assert code == 0
    assert body["data"]["run_id"] == "run-json"
    task = runtime.tasks[0]
    assert task.max_steps == 9
    assert task.timeout_seconds == 12
    assert len(task.assertions) == 1
    assert task.assertions[0].expected == "Submitted"


def test_task_invalid_json_is_structured_and_returns_usage_code() -> None:
    code, body, _ = _invoke(["task", "--task-json", "{not-json"], store=FakeStore())

    assert code == 2
    assert body["success"] is False
    assert body["error"]["code"] == "validation_error"
    assert "JSON" in body["error"]["message"]


def test_task_runtime_exception_is_structured_without_persisting() -> None:
    class BrokenRuntime:
        async def run(self, _task: Any) -> Any:
            raise RuntimeError("provider unavailable")

    store = FakeStore()
    code, body, _ = _invoke(
        ["task", "--goal", "run smoke"], runtime=BrokenRuntime(), store=store
    )

    assert code == 1
    assert body["success"] is False
    assert body["error"]["code"] == "runtime_error"
    assert store.appended == []
    assert "provider unavailable" in body["error"]["message"]


def test_failed_result_keeps_primary_exit_code_when_storage_fails() -> None:
    class BrokenStore:
        def append_result(self, _result: Any) -> str:
            raise OSError("database locked")

    runtime = FakeRuntime(
        {
            "run_id": "run-failed",
            "status": "failed",
            "failure_kind": "assertion",
            "answer_summary": "status remained Pending",
        }
    )

    code, body, _ = _invoke(
        ["task", "--goal", "submit order"], runtime=runtime, store=BrokenStore()
    )

    assert code == 1
    assert body["success"] is False
    assert body["data"]["run_id"] == "run-failed"
    assert body["data"]["status"] == "failed"
    assert body["meta"]["storage_warning"] == "persistence_failed"


def test_dashboard_uses_loopback_default_and_does_not_write_runs() -> None:
    calls: list[tuple[Any, str, int]] = []
    store = FakeStore()

    def runner(app: Any, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    code, body, _ = _invoke(
        ["dashboard", "--port", "8877"],
        store=store,
        uvicorn_runner=runner,
    )

    assert code == 0
    assert body["success"] is True
    assert body["data"]["url"] == "http://127.0.0.1:8877"
    assert calls and calls[0][1:] == ("127.0.0.1", 8877)
    assert store.appended == []


def test_dashboard_announces_url_before_blocking_runner() -> None:
    observed: list[str] = []
    stdout = io.StringIO()
    stderr = io.StringIO()

    def runner(_app: Any, *, host: str, port: int) -> None:
        observed.append(stderr.getvalue())
        assert (host, port) == ("127.0.0.1", 8878)

    code = main(
        ["dashboard", "--port", "8878"],
        store=FakeStore(),
        uvicorn_runner=runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert observed and "http://127.0.0.1:8878" in observed[0]


def test_dashboard_open_is_opt_in_and_receives_actual_url() -> None:
    opened: list[str] = []

    def runner(_app: Any, *, host: str, port: int) -> None:
        assert host == "127.0.0.1"
        assert port == 8766

    code, body, _ = _invoke(
        ["dashboard", "--port", "8766", "--open"],
        store=FakeStore(),
        uvicorn_runner=runner,
        browser_opener=opened.append,
    )

    assert code == 0
    assert body["data"]["url"] == "http://127.0.0.1:8766"
    assert opened == ["http://127.0.0.1:8766"]


def test_dashboard_rejects_out_of_range_port_with_code_two() -> None:
    code, body, _ = _invoke(["dashboard", "--port", "70000"], store=FakeStore())

    assert code == 2
    assert body["success"] is False
    assert body["error"]["code"] == "usage_error"


def test_dashboard_rejects_remote_host_without_explicit_opt_in() -> None:
    code, body, stderr = _invoke(
        ["dashboard", "--host", "0.0.0.0"],
        store=FakeStore(),
        uvicorn_runner=lambda **_: None,
    )

    assert code == 2
    assert body["error"]["code"] == "unsafe_host"
    assert stderr == "non-loopback dashboard hosts require --allow-remote; use SSH tunneling or add authentication first\n"


def test_dashboard_remote_host_requires_explicit_warning_and_flag() -> None:
    calls: list[tuple[str, int]] = []

    def runner(_app: Any, *, host: str, port: int) -> None:
        calls.append((host, port))

    code, body, stderr = _invoke(
        ["dashboard", "--host", "0.0.0.0", "--allow-remote", "--port", "8777"],
        store=FakeStore(),
        uvicorn_runner=runner,
    )

    assert code == 0
    assert body["data"]["url"] == "http://0.0.0.0:8777"
    assert calls == [("0.0.0.0", 8777)]
    assert "no authentication" in stderr


def test_dashboard_runner_internal_type_error_is_not_retried() -> None:
    calls = 0

    def broken_runner(_app: Any, *, host: str, port: int) -> None:
        nonlocal calls
        calls += 1
        raise TypeError("runner bug")

    code, body, _ = _invoke(
        ["dashboard"],
        store=FakeStore(),
        uvicorn_runner=broken_runner,
    )

    assert code == 1
    assert body["success"] is False
    assert body["error"]["code"] == "dashboard_error"
    assert calls == 1


def test_task_result_is_redacted_before_cli_output() -> None:
    runtime = FakeRuntime(
        {
            "run_id": "safe-run",
            "status": "success",
            "failure_kind": "none",
            "answer_summary": "password=secret",
            "url": "https://shop.test/order?token=secret#receipt",
        }
    )
    code, body, _ = _invoke(["task", "--goal", "inspect"], runtime=runtime)

    assert code == 0
    encoded = json.dumps(body, ensure_ascii=False)
    assert "password=secret" not in encoded
    assert "?token" not in encoded


def test_demo_flag_runs_without_external_model_or_browser() -> None:
    code, body, _ = _invoke(["task", "--demo", "--goal", "show demo"])

    assert code == 0
    assert body["success"] is True
    assert body["data"]["status"] == "success"


def test_cli_help_is_structured_and_does_not_start_runtime() -> None:
    code, body, stderr = _invoke(["task", "--help"])

    assert code == 0
    assert body["success"] is True
    assert "--demo" in body["data"]["help"]
    assert stderr == ""
