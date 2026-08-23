"""Contract tests for the TaskLens HTTP surface.

The tests use in-memory fakes so the dashboard can be exercised before the
SQLite repository and Agent Runtime are wired into the application.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from browser_harness.dashboard import create_app


class FakeStore:
    def __init__(self) -> None:
        self.runs = [
            {
                "run_id": "run-1",
                "task_name": "checkout",
                "status": "failed",
                "failure_kind": "assertion",
                "browser_backend": "local",
                "duration_seconds": 2.5,
                "steps": [
                    {
                        "sequence": 1,
                        "helper": "click",
                        "status": "failed",
                        "duration_seconds": 2.5,
                        "failure_kind": "assertion",
                    }
                ],
            },
            {
                "run_id": "run-2",
                "task_name": "search",
                "status": "success",
                "failure_kind": "none",
                "browser_backend": "cdp",
                "duration_seconds": 1.0,
                "steps": [],
            },
        ]
        self.last_filters: dict[str, Any] = {}

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "storage": "sqlite"}

    def summary(self) -> dict[str, Any]:
        return {
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "average_duration_seconds": 1.75,
        }

    def list_runs(
        self,
        *,
        limit: int = 50,
        status: str | None = None,
        browser: str | None = None,
    ) -> list[dict[str, Any]]:
        self.last_filters = {"limit": limit, "status": status, "browser": browser}
        result = self.runs
        if status:
            result = [run for run in result if run["status"] == status]
        if browser:
            result = [run for run in result if run["browser_backend"] == browser]
        return result[:limit]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return next((run for run in self.runs if run["run_id"] == run_id), None)


class BrokenStore(FakeStore):
    def health(self) -> dict[str, Any]:
        raise OSError("database is unavailable")

    def list_runs(self, **_: Any) -> list[dict[str, Any]]:
        raise OSError("database is unavailable")


class SecretStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.runs[0]["goal"] = "inspect https://shop.test/order?token=secret"
        self.runs[0]["error_summary"] = "password=hunter2"
        self.runs[0]["steps"][0]["args_summary"] = '{"authorization":"bearer-secret"}'


class NonFiniteStore(FakeStore):
    def get_run(self, run_id: str) -> dict[str, Any] | None:
        result = super().get_run(run_id)
        if result is not None:
            result = {**result, "duration_seconds": float("inf")}
        return result


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> TestClient:
    async def runtime_factory(task: Any) -> dict[str, Any]:
        return {
            "run_id": "run-created",
            "status": "success",
            "failure_kind": "none",
            "answer_summary": task.goal,
        }

    return TestClient(create_app(store=store, runtime_factory=runtime_factory))


def test_home_is_self_contained_dashboard(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "TaskLens" in response.text
    assert "/api/runs" in response.text
    assert "First deviation" in response.text
    assert "Started" in response.text
    assert "min-width:620px" not in response.text
    assert "text/html" in response.headers["content-type"]


def test_favicon_is_a_quiet_empty_response(client: TestClient) -> None:
    assert client.get("/favicon.ico").status_code == 204


def test_health_and_summary_use_success_envelope(client: TestClient) -> None:
    health = client.get("/api/health")
    summary = client.get("/api/summary")

    assert health.status_code == 200
    assert health.json() == {
        "success": True,
        "data": {"status": "ok", "storage": "sqlite"},
        "error": None,
        "meta": {},
    }
    assert summary.json()["success"] is True
    assert summary.json()["data"]["total"] == 2


def test_runs_support_bounded_filters_and_detail(client: TestClient, store: FakeStore) -> None:
    response = client.get("/api/runs?limit=1&status=failed&browser=local")

    assert response.status_code == 200
    assert response.json()["data"] == [store.runs[0]]
    assert store.last_filters == {"limit": 1, "status": "failed", "browser": "local"}

    detail = client.get("/api/runs/run-1")
    assert detail.status_code == 200
    assert detail.json()["data"]["steps"][0]["helper"] == "click"


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=101", "status=running", "browser=remote&limit=1"],
)
def test_runs_reject_invalid_query_parameters(client: TestClient, query: str) -> None:
    response = client.get(f"/api/runs?{query}")

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "validation_error"


def test_missing_run_is_structured_404(client: TestClient) -> None:
    response = client.get("/api/runs/not-found")

    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "data": None,
        "error": {"code": "not_found", "message": "Run 'not-found' was not found"},
        "meta": {},
    }


def test_task_endpoint_validates_and_invokes_single_runtime(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        json={
            "task_id": "checkout-smoke",
            "goal": "Complete checkout",
            "constraints": {"max_steps": 5, "timeout_seconds": 30},
            "assertions": [],
            "retry_policy": {"max_retries": 0, "idempotent_actions_only": True},
        },
    )

    assert response.status_code == 202
    assert response.json()["success"] is True
    assert response.json()["data"]["run_id"] == "run-created"


@pytest.mark.parametrize(
    "payload",
    [{}, {"goal": ""}, {"goal": "x", "constraints": {"max_steps": 0}}],
)
def test_task_endpoint_returns_400_for_invalid_payload(
    client: TestClient, payload: dict[str, Any]
) -> None:
    response = client.post("/api/tasks", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_malformed_json_returns_structured_400(client: TestClient) -> None:
    response = client.post(
        "/api/tasks",
        content="{not-json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_json"


def test_storage_failure_is_degraded_without_stack_trace() -> None:
    client = TestClient(create_app(store=BrokenStore()))

    health = client.get("/api/health")
    runs = client.get("/api/runs")

    assert health.status_code == 200
    assert health.json()["success"] is True
    assert health.json()["data"]["status"] == "degraded"
    assert runs.status_code == 503
    assert "Traceback" not in runs.text


def test_store_list_internal_type_error_is_not_retried() -> None:
    class BrokenListStore(FakeStore):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def list_runs(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            del args, kwargs
            self.calls += 1
            raise TypeError("repository bug")

    store = BrokenListStore()
    client = TestClient(create_app(store=store))

    response = client.get("/api/runs")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "storage_unavailable"
    assert store.calls == 1


def test_history_is_read_only(client: TestClient) -> None:
    response = client.delete("/api/runs/run-1")

    assert response.status_code == 405
    assert response.json()["success"] is False
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_unknown_route_uses_the_same_error_envelope(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_default_runtime_factory_supports_explicit_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKLENS_MODEL_PROVIDER", "demo")
    store = FakeStore()
    client = TestClient(create_app(store=store))

    response = client.post("/api/tasks", json={"task_id": "demo", "goal": "show demo"})

    assert response.status_code == 202
    assert response.json()["success"] is True
    assert response.json()["data"]["status"] == "success"


def test_invalid_assertion_dsl_is_rejected_before_runtime() -> None:
    calls: list[Any] = []

    async def runtime_factory(task: Any) -> dict[str, Any]:
        calls.append(task)
        return {"run_id": "unexpected", "status": "success"}

    client = TestClient(create_app(store=FakeStore(), runtime_factory=runtime_factory))
    response = client.post(
        "/api/tasks",
        json={"task_id": "invalid", "goal": "check", "assertions": [{"type": "bogus"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
    assert calls == []


def test_oversized_assertion_value_is_rejected_at_http_boundary() -> None:
    client = TestClient(create_app(store=FakeStore(), runtime_factory=lambda _task: {}))
    response = client.post(
        "/api/tasks",
        json={
            "task_id": "large",
            "goal": "check",
            "assertions": [{"type": "structured", "expected": "x" * 40_000}],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_history_endpoints_redact_untrusted_repository_values() -> None:
    client = TestClient(create_app(store=SecretStore()))

    list_response = client.get("/api/runs")
    detail_response = client.get("/api/runs/run-1")

    for response in (list_response, detail_response):
        assert response.status_code == 200
        encoded = response.text
        assert "hunter2" not in encoded
        assert "bearer-secret" not in encoded
        assert "?token=secret" not in encoded


def test_detail_converts_non_finite_numbers_to_json_null() -> None:
    client = TestClient(create_app(store=NonFiniteStore()))

    response = client.get("/api/runs/run-1")

    assert response.status_code == 200
    assert response.json()["data"]["duration_seconds"] is None


def test_factory_internal_type_error_is_not_retried() -> None:
    calls = 0

    def broken_factory(_task: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise TypeError("factory bug")

    client = TestClient(create_app(store=FakeStore(), runtime_factory=broken_factory))
    response = client.post("/api/tasks", json={"task_id": "t", "goal": "g"})

    assert response.status_code == 500
    assert calls == 1


def test_api_retry_budget_matches_runtime_contract() -> None:
    captured: list[Any] = []

    async def runtime_factory(task: Any) -> dict[str, Any]:
        captured.append(task)
        return {"run_id": "retry", "status": "success"}

    client = TestClient(create_app(store=FakeStore(), runtime_factory=runtime_factory))
    response = client.post(
        "/api/tasks",
        json={
            "task_id": "retry",
            "goal": "check",
            "retry_policy": {"max_retries": 20},
        },
    )

    assert response.status_code == 202
    assert captured[0].retry_policy.max_retries == 20
