"""Contract tests for TaskLens evidence models and SQLite repository.

These tests intentionally exercise the public data-layer contract rather than
the implementation details.  The runtime and dashboard can therefore use the
same records without depending on SQLite internals.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from browser_harness.observability import (
    AssertionRecord,
    FailureKind,
    RunRecord,
    StepRecord,
    find_first_deviation_step,
    sanitize_mapping,
    sanitize_text,
    sanitize_url,
)
from browser_harness.storage import SQLiteRepository, StorageError


def _run(*, steps: list[StepRecord] | None = None, **overrides) -> RunRecord:
    values = {
        "run_id": "run-1",
        "goal": "Complete checkout with token=should-not-persist",
        "started_at": datetime(2026, 8, 23, 1, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 23, 1, 0, 3, 120000, tzinfo=UTC),
        "duration_seconds": 3.12,
        "status": "failed",
        "failure_kind": FailureKind.ASSERTION,
        "first_deviation_step": 2,
        "browser_backend": "local",
        "exit_code": 1,
        "output_tail": "Authorization: Bearer top-secret",
        "error_summary": "checkout failed with password=hunter2",
        "max_steps": 20,
        "steps": steps or [],
    }
    values.update(overrides)
    return RunRecord.model_validate(values)


def _steps() -> list[StepRecord]:
    return [
        StepRecord(
            sequence=1,
            helper="goto",
            observation_summary="URL=https://shop.test/cart?token=secret",
            action="goto",
            args_summary="https://shop.test/cart?token=secret",
            duration_seconds=0.2,
            status="success",
        ),
        StepRecord(
            sequence=2,
            helper="click",
            observation_summary="Order status: Pending",
            action="click",
            args_summary='{"authorization":"secret"}',
            duration_seconds=0.4,
            status="failed",
            failure_kind=FailureKind.ASSERTION,
            error_summary="expected Submitted, password=hunter2",
            assertions=[
                AssertionRecord(
                    name="order-status",
                    assertion_type="text",
                    status="failed",
                    expected="Submitted",
                    actual="Pending",
                    evidence="[data-testid=order-status]",
                )
            ],
        ),
    ]


def test_sanitize_url_drops_query_and_fragment() -> None:
    assert sanitize_url("https://shop.test/cart?token=abc&item=1#order") == (
        "https://shop.test/cart"
    )
    assert sanitize_url("/cart?password=abc#fragment") == "/cart"


def test_sanitize_text_redacts_bearer_key_values_and_bounds_length() -> None:
    raw = "Authorization: Bearer abc123 password=hunter2 https://x.test/a?token=abc#frag " + "x" * 100
    safe = sanitize_text(raw, max_length=80)
    assert "abc123" not in safe
    assert "hunter2" not in safe
    assert "?token" not in safe
    assert "#frag" not in safe
    assert len(safe) <= 80


def test_sanitize_mapping_redacts_sensitive_keys_recursively() -> None:
    safe = sanitize_mapping(
        {
            "token": "abc",
            "nested": {"api_key": "def", "name": "Alice"},
            "items": [{"cookie": "session-cookie", "url": "https://x.test/p?q=1"}],
        }
    )
    assert safe["token"] == "[REDACTED]"
    assert safe["nested"]["api_key"] == "[REDACTED]"
    assert safe["nested"]["name"] == "Alice"
    assert safe["items"][0]["cookie"] == "[REDACTED]"
    assert safe["items"][0]["url"] == "https://x.test/p"


def test_sanitize_mapping_redacts_values_from_type_actions() -> None:
    safe = sanitize_mapping(
        {
            "action": {"kind": "type", "target": "#password", "value": "hunter2"},
            "normal": {"kind": "goto", "url": "https://x.test"},
        }
    )

    assert safe["action"]["value"] == "[REDACTED]"
    assert safe["normal"]["url"] == "https://x.test"


def test_models_bound_and_redact_fields_at_the_boundary() -> None:
    record = _run(
        goal="g" * 500,
        output_tail="o" * 5000,
        error_summary="password=secret " + "e" * 2000,
        steps=[
            StepRecord(
                sequence=1,
                helper="x",
                observation_summary="a" * 5000,
                args_summary="b" * 1000,
                error_summary="token=abc " + "c" * 2000,
            )
        ],
    )
    assert len(record.goal) <= 240
    assert len(record.output_tail) <= 2000
    assert "secret" not in record.error_summary
    assert len(record.steps[0].observation_summary) <= 2000
    assert len(record.steps[0].args_summary) <= 300


def test_first_deviation_returns_earliest_failed_action_or_assertion() -> None:
    steps = [
        StepRecord(sequence=1, helper="wait", status="success"),
        StepRecord(
            sequence=2,
            helper="click",
            status="success",
            assertions=[AssertionRecord(name="visible", status="failed")],
        ),
        StepRecord(sequence=3, helper="type", status="failed", failure_kind="action"),
    ]
    assert find_first_deviation_step(steps) == 2


def test_repository_round_trip_filters_and_nested_evidence(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "tasklens.db")
    record = _run(steps=_steps())

    assert repo.append_run(record) == "run-1"
    detail = repo.get_run("run-1")
    assert detail is not None
    assert detail["run_id"] == "run-1"
    assert detail["goal"].find("should-not-persist") == -1
    assert len(detail["steps"]) == 2
    assert detail["steps"][1]["assertions"][0]["status"] == "failed"
    assert "secret" not in str(detail)

    assert len(repo.list_runs(status="failed", browser_backend="local")) == 1
    assert repo.list_runs(status="success") == []
    summary = repo.summary()
    assert summary["total_runs"] == 1
    assert summary["failed_count"] == 1
    assert summary["average_duration_seconds"] == pytest.approx(3.12)


def test_repository_append_step_and_finish_run(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "tasklens.db")
    running = _run(
        status="running",
        failure_kind="none",
        finished_at=None,
        duration_seconds=None,
        steps=[],
    )
    repo.start_run(running)
    repo.append_step("run-1", _steps()[0])
    repo.finish_run(
        _run(
            status="success",
            failure_kind="none",
            exit_code=0,
            first_deviation_step=None,
            steps=[],
        )
    )
    detail = repo.get_run("run-1")
    assert detail is not None
    assert detail["status"] == "success"
    assert len(detail["steps"]) == 1


def test_repository_rolls_back_an_invalid_nested_write(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "tasklens.db")
    valid = _run(steps=_steps())
    repo.append_run(valid)

    # A duplicate step sequence violates the composite primary key.  The
    # failed transaction must not leave a partial assertion/step behind.
    with pytest.raises(StorageError):
        repo.append_step("run-1", _steps()[0])
    assert len(repo.get_run("run-1")["steps"]) == 2


def test_repository_write_failure_is_raised_for_fail_open_caller(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "tasklens.db")
    repo.close()
    with pytest.raises(StorageError):
        repo.append_run(_run())


def test_repository_safe_append_preserves_primary_result_on_failure(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "tasklens.db")
    repo.close()
    assert repo.append_run_safe(_run()) is False
    assert repo.last_error is not None


def test_schema_uses_foreign_keys_and_expected_tables(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "tasklens.db")
    tables = {
        row[0]
        for row in repo.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"runs", "steps", "assertions", "schema_meta"}.issubset(tables)
    assert repo.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_repository_does_not_close_caller_owned_connection() -> None:
    connection = sqlite3.connect(":memory:")
    repo = SQLiteRepository(connection=connection)

    repo.close()

    assert connection.execute("SELECT 1").fetchone()[0] == 1
    connection.close()


def test_default_health_path_matches_default_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BH_CONFIG_DIR", str(tmp_path))
    repo = SQLiteRepository()

    assert repo.health()["path"] == str(tmp_path / "tasklens.db")
    repo.close()
