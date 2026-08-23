"""Cross-module contracts for the first runnable TaskLens slice."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from browser_harness.agent_runtime import AgentRuntime
from browser_harness.dashboard import create_app
from browser_harness.storage import SQLiteRepository
from browser_harness.tasklens_cli import persist_result
from browser_harness.tasklens_domain import (
    Action,
    ActionOutcome,
    Assertion,
    Observation,
    TaskSpec,
)


class _Runtime:
    async def run(self, task):
        return {
            "run_id": "integration-run",
            "task_id": task.task_id,
            "status": "success",
            "failure_kind": "none",
            "answer_summary": "verified",
            "duration_seconds": 0.25,
            "steps": [],
        }


def test_task_endpoint_persists_agent_result_and_dashboard_can_query(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "tasklens.db")
    client = TestClient(create_app(store=repository, runtime=_Runtime()))

    response = client.post(
        "/api/tasks",
        json={
            "task_id": "smoke",
            "goal": "verify the page",
            "constraints": {"max_steps": 3, "timeout_seconds": 10},
        },
    )

    assert response.status_code == 202
    assert response.json()["data"]["run_id"] == "integration-run"
    detail = client.get("/api/runs/integration-run")
    assert detail.status_code == 200
    assert detail.json()["data"]["status"] == "success"


def test_task_endpoint_keeps_success_when_observation_write_fails() -> None:
    class BrokenRepository:
        def summary(self):
            return {"total_runs": 0}

        def health(self):
            return {"status": "degraded", "storage": "broken"}

        def append_run_safe(self, _record):
            return False

        def list_runs(self, **_kwargs):
            return []

        def get_run(self, _run_id):
            return None

    client = TestClient(create_app(store=BrokenRepository(), runtime=_Runtime()))
    response = client.post("/api/tasks", json={"task_id": "smoke", "goal": "verify"})

    assert response.status_code == 202
    assert response.json()["success"] is True


def test_real_runtime_result_can_be_adapted_into_queryable_evidence(tmp_path: Path) -> None:
    class Browser:
        def __init__(self) -> None:
            self.after_click = False

        def observe(self):
            return Observation(text="Submitted" if self.after_click else "Checkout")

        def execute(self, _action):
            self.after_click = True
            return ActionOutcome(success=True)

    class Model:
        def next_action(self, _task, _observation, _history):
            return Action(kind="click", target="#submit", idempotent=False)

    task = TaskSpec(
        task_id="runtime-smoke",
        goal="submit order",
        max_steps=2,
        assertions=[Assertion(type="text", expected="Submitted")],
    )
    result = AgentRuntime(Browser(), Model()).run_sync(task)
    repository = SQLiteRepository(tmp_path / "tasklens.db")

    assert result.status == "success"
    assert persist_result(repository, result, task)[0] is True
    detail = repository.get_run(result.run_id)
    assert detail is not None
    assert detail["status"] == "success"
    assert detail["steps"][0]["assertions"][0]["status"] == "passed"


def test_task_api_redacts_sensitive_result_fields_before_returning_them() -> None:
    class SensitiveRuntime:
        async def run(self, task):
            return {
                "run_id": "sensitive-run",
                "status": "success",
                "failure_kind": "none",
                "answer_summary": "token=do-not-return",
                "credentials": {"password": "secret"},
                "url": "https://shop.test/order?token=secret#receipt",
            }

    client = TestClient(create_app(store=None, runtime=SensitiveRuntime()))
    response = client.post("/api/tasks", json={"task_id": "safe", "goal": "inspect"})

    assert response.status_code == 202
    body = response.text
    assert "do-not-return" not in body
    assert "password\\\":\\\"secret" not in body
    assert "?token" not in body
