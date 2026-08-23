"""SQLite evidence repository for TaskLens.

The repository is intentionally small and synchronous: the first TaskLens
slice runs in one local process, and a transaction per run keeps evidence
consistent.  Storage errors are surfaced as :class:`StorageError`; callers
that must preserve the browser task's result can use ``append_run_safe`` (or
catch the error) to implement the documented fail-open boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Self

from . import paths
from .observability import (
    AssertionRecord,
    RunRecord,
    StepRecord,
    build_run_record,
    sanitize_text,
    sanitize_value,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_HISTORY = 10_000
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 100


class StorageError(RuntimeError):
    """A repository operation could not be completed.

    The exception deliberately carries only a bounded, redacted message.  It
    is safe for a caller to log it or expose a generic health status.
    """

    def __init__(self, message: str, *, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(sanitize_text(message, 500))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _decode_timestamp(value: str | None) -> str | None:
    # API consumers use ISO strings; keeping this helper named makes the
    # conversion boundary explicit and leaves malformed legacy rows readable.
    return value


def _json(value: Any) -> str:
    safe = sanitize_value(value, max_length=500)
    try:
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(sanitize_text(safe, 500), ensure_ascii=False)


def _from_json(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return sanitize_text(value, 500)


class SQLiteRepository:
    """Append/query TaskLens run evidence in a local SQLite database.

    Parameters
    ----------
    database:
        Path to a SQLite file.  ``None`` uses the browser-harness config
        directory.  ``":memory:"`` is useful for isolated tests.
    max_history:
        Maximum number of top-level runs retained.  Oldest runs are pruned
        after a successful append; child rows are removed by foreign-key
        cascade.
    connection:
        Optional existing sqlite connection, primarily for embedding/tests.
    """

    def __init__(
        self,
        database: str | Path | None = None,
        *,
        max_history: int = DEFAULT_MAX_HISTORY,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if max_history < 1:
            raise ValueError("max_history must be positive")
        self.database = database
        self.max_history = max_history
        self._lock = RLock()
        self._last_error: str | None = None
        self._owns_connection = connection is None
        self._connection: sqlite3.Connection | None = None

        try:
            if connection is not None:
                self._connection = connection
            else:
                path = self._resolve_path(database)
                if path != ":memory:":
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                self._connection = sqlite3.connect(
                    path,
                    check_same_thread=False,
                    isolation_level=None,
                )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            # WAL is unavailable for :memory:, and a failure to enable it
            # should not prevent an otherwise valid local repository.
            try:
                self._connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError:
                pass
            self._initialize_schema()
        except Exception as exc:
            self._record_error(exc)
            raise StorageError("unable to initialize SQLite repository", cause=exc) from exc

    @staticmethod
    def _resolve_path(database: str | Path | None) -> str:
        if database is None:
            # Keep the CLI and dashboard on one evidence file.  A single
            # default path is part of the local-first run/query contract.
            return str(paths.config_dir() / "tasklens.db")
        return str(database)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("SQLite repository is closed")
        return self._connection

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _record_error(self, exc: BaseException) -> None:
        self._last_error = sanitize_text(f"{type(exc).__name__}: {exc}", 500)

    def _initialize_schema(self) -> None:
        conn = self.connection
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value)
                VALUES ('schema_version', '1')
                ON CONFLICT(key) DO UPDATE SET value=excluded.value;

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                task_name TEXT NOT NULL DEFAULT '',
                goal TEXT NOT NULL DEFAULT '',
                task_preview TEXT NOT NULL DEFAULT '',
                task_length INTEGER,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                duration_seconds REAL,
                status TEXT NOT NULL,
                failure_kind TEXT NOT NULL,
                first_deviation_step INTEGER,
                browser_backend TEXT NOT NULL DEFAULT 'unknown',
                exit_code INTEGER,
                output_tail TEXT NOT NULL DEFAULT '',
                error_summary TEXT,
                answer_summary TEXT NOT NULL DEFAULT '',
                max_steps INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS idx_runs_browser ON runs(browser_backend);

            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                helper TEXT NOT NULL,
                observation_summary TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                args_summary TEXT NOT NULL DEFAULT '',
                duration_seconds REAL,
                status TEXT NOT NULL,
                failure_kind TEXT NOT NULL,
                error_summary TEXT,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                started_at TEXT,
                finished_at TEXT,
                PRIMARY KEY (run_id, sequence),
                FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_steps_duration
                ON steps(run_id, duration_seconds DESC);

            CREATE TABLE IF NOT EXISTS assertions (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                name TEXT NOT NULL,
                assertion_type TEXT NOT NULL,
                status TEXT NOT NULL,
                operator TEXT,
                expected_json TEXT,
                actual_json TEXT,
                evidence_json TEXT,
                error_summary TEXT,
                PRIMARY KEY (run_id, sequence, ordinal),
                FOREIGN KEY (run_id, sequence)
                    REFERENCES steps(run_id, sequence) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_assertions_status
                ON assertions(run_id, status);
            """
        )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connection
        with self._lock:
            try:
                conn.execute("BEGIN")
                yield conn
                conn.execute("COMMIT")
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                self._record_error(exc)
                if isinstance(exc, StorageError):
                    raise
                raise StorageError("SQLite transaction failed", cause=exc) from exc

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    if self._owns_connection:
                        self._connection.close()
                finally:
                    self._connection = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        """Best-effort cleanup for short-lived CLI/test repositories."""

        try:
            if getattr(self, "_owns_connection", False):
                self.close()
        except (AttributeError, sqlite3.Error):
            # Destructors run during interpreter shutdown; cleanup must never
            # mask the primary task result.
            return

    def _run_values(self, record: RunRecord) -> tuple[Any, ...]:
        return (
            record.run_id,
            record.task_name,
            record.goal,
            record.task_preview,
            record.task_length,
            _timestamp(record.started_at),
            _timestamp(record.finished_at),
            record.duration_seconds,
            record.status,
            record.failure_kind,
            record.first_deviation_step,
            record.browser_backend,
            record.exit_code,
            record.output_tail,
            record.error_summary,
            record.answer_summary,
            record.max_steps,
            _timestamp(_utc_now()),
        )

    def _insert_run(self, conn: sqlite3.Connection, record: RunRecord) -> None:
        conn.execute(
            """
            INSERT INTO runs(
                run_id, task_name, goal, task_preview, task_length, started_at,
                finished_at, duration_seconds, status, failure_kind,
                first_deviation_step, browser_backend, exit_code, output_tail,
                error_summary, answer_summary, max_steps, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._run_values(record),
        )

    def _insert_step(self, conn: sqlite3.Connection, run_id: str, step: StepRecord) -> None:
        conn.execute(
            """
            INSERT INTO steps(
                run_id, sequence, helper, observation_summary, action,
                args_summary, duration_seconds, status, failure_kind,
                error_summary, evidence_refs_json, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                step.sequence,
                step.helper,
                step.observation_summary,
                step.action,
                step.args_summary,
                step.duration_seconds,
                step.status,
                step.failure_kind,
                step.error_summary,
                _json(step.evidence_refs),
                _timestamp(step.started_at),
                _timestamp(step.finished_at),
            ),
        )
        for ordinal, assertion in enumerate(step.assertions):
            self._insert_assertion(conn, run_id, step.sequence, ordinal, assertion)

    def _insert_assertion(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        sequence: int,
        ordinal: int,
        assertion: AssertionRecord,
    ) -> None:
        conn.execute(
            """
            INSERT INTO assertions(
                run_id, sequence, ordinal, name, assertion_type, status,
                operator, expected_json, actual_json, evidence_json, error_summary
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                sequence,
                ordinal,
                assertion.name,
                assertion.assertion_type,
                assertion.status,
                assertion.operator,
                _json(assertion.expected),
                _json(assertion.actual),
                _json(assertion.evidence),
                assertion.error_summary,
            ),
        )

    def _prune_locked(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()
        count = int(row["count"] if row is not None else 0)
        overflow = count - self.max_history
        if overflow <= 0:
            return
        conn.execute(
            """
            DELETE FROM runs WHERE run_id IN (
                SELECT run_id FROM runs ORDER BY started_at ASC, run_id ASC LIMIT ?
            )
            """,
            (overflow,),
        )

    def start_run(self, record: RunRecord | Mapping[str, Any]) -> str:
        """Insert a running record without child steps."""

        normalized = build_run_record(record)
        with self._transaction() as conn:
            self._insert_run(conn, normalized)
        return normalized.run_id

    def append_run(self, record: RunRecord | Mapping[str, Any]) -> str:
        """Atomically append a run and all nested steps/assertions."""

        normalized = build_run_record(record)
        with self._transaction() as conn:
            self._insert_run(conn, normalized)
            for step in normalized.steps:
                self._insert_step(conn, normalized.run_id, step)
            self._prune_locked(conn)
        return normalized.run_id

    def append_run_safe(self, record: RunRecord | Mapping[str, Any]) -> bool:
        """Best-effort append for a fail-open observation hook.

        ``False`` means no evidence was persisted; the caller's primary task
        result is intentionally not changed.  The detailed error is available
        through ``last_error`` and ``health()``.
        """

        try:
            self.append_run(record)
            return True
        except Exception as exc:  # noqa: BLE001 - documented fail-open boundary
            self._record_error(exc)
            return False

    def append_step(self, run_id: str, step: StepRecord | Mapping[str, Any]) -> int:
        """Atomically append one step and its assertions to an existing run."""

        normalized = step if isinstance(step, StepRecord) else StepRecord.model_validate(step)
        with self._transaction() as conn:
            exists = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if exists is None:
                raise StorageError(f"unknown run id: {run_id}")
            self._insert_step(conn, run_id, normalized)
        return normalized.sequence

    def finish_run(self, record: RunRecord | Mapping[str, Any]) -> str:
        """Update top-level state and append any supplied child steps.

        Runtime implementations may stream steps with ``append_step`` and pass
        an empty ``steps`` list here, or provide a complete final record.  In
        the latter case already-present steps are left untouched and only new
        sequence numbers are appended.
        """

        normalized = build_run_record(record)
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE runs SET task_name=?, goal=?, task_preview=?, task_length=?,
                    started_at=?, finished_at=?, duration_seconds=?, status=?,
                    failure_kind=?, first_deviation_step=?, browser_backend=?,
                    exit_code=?, output_tail=?, error_summary=?, answer_summary=?,
                    max_steps=? WHERE run_id=?
                """,
                (
                    normalized.task_name,
                    normalized.goal,
                    normalized.task_preview,
                    normalized.task_length,
                    _timestamp(normalized.started_at),
                    _timestamp(normalized.finished_at),
                    normalized.duration_seconds,
                    normalized.status,
                    normalized.failure_kind,
                    normalized.first_deviation_step,
                    normalized.browser_backend,
                    normalized.exit_code,
                    normalized.output_tail,
                    normalized.error_summary,
                    normalized.answer_summary,
                    normalized.max_steps,
                    normalized.run_id,
                ),
            )
            if updated.rowcount != 1:
                raise StorageError(f"unknown run id: {normalized.run_id}")
            existing = {
                row["sequence"]
                for row in conn.execute(
                    "SELECT sequence FROM steps WHERE run_id = ?", (normalized.run_id,)
                ).fetchall()
            }
            for step in normalized.steps:
                if step.sequence not in existing:
                    self._insert_step(conn, normalized.run_id, step)
            self._prune_locked(conn)
        return normalized.run_id

    def _assertion_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "name": row["name"],
            "type": row["assertion_type"],
            "assertion_type": row["assertion_type"],
            "status": row["status"],
            "operator": row["operator"],
            "expected": _from_json(row["expected_json"]),
            "actual": _from_json(row["actual_json"]),
            "evidence": _from_json(row["evidence_json"]),
            "error_summary": row["error_summary"],
        }

    def _step_dict(self, row: sqlite3.Row, assertions: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "sequence": row["sequence"],
            "helper": row["helper"],
            "observation_summary": row["observation_summary"],
            "action": row["action"],
            "args_summary": row["args_summary"],
            "duration_seconds": row["duration_seconds"],
            "status": row["status"],
            "failure_kind": row["failure_kind"],
            "error_summary": row["error_summary"],
            "evidence_refs": _from_json(row["evidence_refs_json"]) or [],
            "started_at": _decode_timestamp(row["started_at"]),
            "finished_at": _decode_timestamp(row["finished_at"]),
            "assertions": assertions,
        }

    def _run_dict(self, row: sqlite3.Row, *, include_steps: bool = False) -> dict[str, Any]:
        result = {
            "run_id": row["run_id"],
            "id": row["run_id"],
            "task_name": row["task_name"],
            "goal": row["goal"],
            "task_preview": row["task_preview"],
            "task_length": row["task_length"],
            "started_at": _decode_timestamp(row["started_at"]),
            "finished_at": _decode_timestamp(row["finished_at"]),
            "duration_seconds": row["duration_seconds"],
            "status": row["status"],
            "failure_kind": row["failure_kind"],
            "first_deviation_step": row["first_deviation_step"],
            "browser_backend": row["browser_backend"],
            "exit_code": row["exit_code"],
            "output_tail": row["output_tail"],
            "error_summary": row["error_summary"],
            "answer_summary": row["answer_summary"],
            "max_steps": row["max_steps"],
            "step_count": 0,
        }
        count_row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM steps WHERE run_id = ?", (row["run_id"],)
        ).fetchone()
        result["step_count"] = int(count_row["count"] if count_row else 0)
        if include_steps:
            step_rows = self.connection.execute(
                "SELECT * FROM steps WHERE run_id = ? ORDER BY sequence", (row["run_id"],)
            ).fetchall()
            assertion_rows = self.connection.execute(
                "SELECT * FROM assertions WHERE run_id = ? ORDER BY sequence, ordinal",
                (row["run_id"],),
            ).fetchall()
            grouped: dict[int, list[dict[str, Any]]] = {}
            for assertion_row in assertion_rows:
                grouped.setdefault(assertion_row["sequence"], []).append(
                    self._assertion_dict(assertion_row)
                )
            result["steps"] = [
                self._step_dict(step_row, grouped.get(step_row["sequence"], []))
                for step_row in step_rows
            ]
        return result

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Return one run with nested steps/assertions, or ``None``."""

        try:
            row = self.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._run_dict(row, include_steps=True) if row is not None else None
        except Exception as exc:
            self._record_error(exc)
            if isinstance(exc, StorageError):
                raise
            raise StorageError("SQLite query failed", cause=exc) from exc

    def list_runs(
        self,
        *,
        limit: int = DEFAULT_LIST_LIMIT,
        status: str | None = None,
        browser_backend: str | None = None,
        browser: str | None = None,
    ) -> list[dict[str, Any]]:
        """List newest runs with optional status/browser filters."""

        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        backend = browser_backend if browser_backend is not None else browser
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if backend:
            clauses.append("browser_backend = ?")
            params.append(str(backend))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            rows = self.connection.execute(
                f"SELECT * FROM runs {where} ORDER BY started_at DESC, run_id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._run_dict(row) for row in rows]
        except Exception as exc:
            self._record_error(exc)
            raise StorageError("SQLite query failed", cause=exc) from exc

    def summary(self) -> dict[str, Any]:
        """Return deterministic aggregate metrics for the dashboard."""

        try:
            row = self.connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status='timeout' THEN 1 ELSE 0 END) AS timed_out,
                    SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                    AVG(duration_seconds) AS average_duration
                FROM runs
                """
            ).fetchone()
            total = int(row["total"] or 0)
            succeeded = int(row["succeeded"] or 0)
            failures = int(row["failed"] or 0)
            timeout = int(row["timed_out"] or 0)
            cancelled = int(row["cancelled"] or 0)
            avg = float(row["average_duration"]) if row["average_duration"] is not None else None
            browsers = {
                item["browser_backend"]: int(item["count"])
                for item in self.connection.execute(
                    "SELECT browser_backend, COUNT(*) AS count FROM runs GROUP BY browser_backend"
                ).fetchall()
            }
            by_failure = {
                item["failure_kind"]: int(item["count"])
                for item in self.connection.execute(
                    "SELECT failure_kind, COUNT(*) AS count FROM runs GROUP BY failure_kind"
                ).fetchall()
            }
            return {
                "total_runs": total,
                "successful_count": succeeded,
                "succeeded_count": succeeded,
                "failed_count": failures,
                "timeout_count": timeout,
                "cancelled_count": cancelled,
                "average_duration_seconds": avg,
                "success_rate": (succeeded / total) if total else None,
                "browser_distribution": browsers,
                "by_failure_kind": by_failure,
            }
        except Exception as exc:
            self._record_error(exc)
            raise StorageError("SQLite summary query failed", cause=exc) from exc

    def health(self) -> dict[str, Any]:
        """Return a non-throwing storage health snapshot."""

        try:
            self.connection.execute("SELECT 1").fetchone()
            return {
                "ok": True,
                "status": "ok",
                "path": str(self.database) if self.database is not None else self._resolve_path(None),
                "last_error": self.last_error,
            }
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            self._record_error(exc)
            return {
                "ok": False,
                "status": "error",
                "path": str(self.database) if self.database is not None else None,
                "last_error": self.last_error,
            }

    def prune(self, max_history: int | None = None) -> int:
        """Delete oldest records until the configured history bound is met."""

        if max_history is not None:
            if max_history < 1:
                raise ValueError("max_history must be positive")
            self.max_history = max_history
        with self._transaction() as conn:
            before = int(conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"])
            self._prune_locked(conn)
            after = int(conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"])
        return before - after


__all__ = ["SQLiteRepository", "StorageError"]
