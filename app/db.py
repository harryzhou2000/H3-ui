from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})
TERMINAL_JOB_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "deleted", "unavailable"}
)
JOB_STATUSES = ACTIVE_JOB_STATUSES | TERMINAL_JOB_STATUSES


class StudioStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    value TEXT NOT NULL,
                    mime_type TEXT,
                    size INTEGER,
                    notes TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    provider_file_id TEXT,
                    provider_expires_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    task_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_json TEXT,
                    response_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    downloaded_filename TEXT
                );
                CREATE TABLE IF NOT EXISTS submissions (
                    client_request_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    task_id TEXT,
                    request_json TEXT NOT NULL,
                    error_json TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    @staticmethod
    def _asset(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["tags"] = json.loads(data.pop("tags_json"))
        return data

    def list_assets(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM assets ORDER BY created_at DESC, name").fetchall()
        return [self._asset(row) for row in rows]

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return self._asset(row) if row else None

    def insert_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO assets (
                    id, kind, name, source_type, value, mime_type, size,
                    notes, tags_json, created_at, provider_file_id, provider_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset["id"], asset["kind"], asset["name"], asset["source_type"],
                    asset["value"], asset.get("mime_type"), asset.get("size"),
                    asset.get("notes", ""), json.dumps(asset.get("tags", [])),
                    asset.get("created_at", int(time.time())), asset.get("provider_file_id"),
                    asset.get("provider_expires_at"),
                ),
            )
        return self.get_asset(asset["id"])  # type: ignore[return-value]

    def update_asset(self, asset_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name", "notes", "tags_json", "provider_file_id", "provider_expires_at"
        }
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            db_key = "tags_json" if key == "tags" else key
            if db_key not in allowed:
                continue
            updates.append(f"{db_key} = ?")
            values.append(json.dumps(value) if key == "tags" else value)
        if updates:
            values.append(asset_id)
            with self._connect() as db:
                db.execute(f"UPDATE assets SET {', '.join(updates)} WHERE id = ?", values)
        return self.get_asset(asset_id)

    def delete_asset(self, asset_id: str) -> dict[str, Any] | None:
        asset = self.get_asset(asset_id)
        if asset:
            with self._connect() as db:
                db.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
        return asset

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["request"] = json.loads(data.pop("request_json")) if data["request_json"] else None
        data["response"] = (
            json.loads(data.pop("response_json")) if data["response_json"] else None
        )
        return data

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        return [self._job(row) for row in rows]

    def get_job(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE task_id = ?", (task_id,)).fetchone()
        return self._job(row) if row else None

    def upsert_job(
        self,
        task_id: str,
        operation: str,
        status: str,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        created_at: int | None = None,
        *,
        force_status: bool = False,
    ) -> dict[str, Any]:
        if status not in JOB_STATUSES:
            raise ValueError(f"Unsupported local job status: {status}")
        now = int(time.time())
        created_at_value = created_at or now
        request_json = json.dumps(request) if request is not None else None
        response_json = json.dumps(response) if response is not None else None
        preserve_existing = """
            jobs.status IN ('succeeded', 'failed', 'cancelled', 'deleted', 'unavailable')
            OR (jobs.status = 'running' AND excluded.status = 'queued')
        """
        with self._connect() as db:
            db.execute(
                f"""
                INSERT INTO jobs (
                    task_id, operation, status, request_json, response_json,
                    created_at, updated_at, downloaded_filename
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    operation = excluded.operation,
                    status = CASE
                        WHEN ? THEN excluded.status
                        WHEN {preserve_existing} THEN jobs.status
                        ELSE excluded.status
                    END,
                    request_json = COALESCE(excluded.request_json, jobs.request_json),
                    response_json = CASE
                        WHEN NOT ? AND ({preserve_existing}) THEN jobs.response_json
                        ELSE COALESCE(excluded.response_json, jobs.response_json)
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    operation or "generation",
                    status,
                    request_json,
                    response_json,
                    created_at_value,
                    now,
                    None,
                    force_status,
                    force_status,
                ),
            )
        return self.get_job(task_id)  # type: ignore[return-value]

    def mark_downloaded(self, task_id: str, filename: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE jobs SET downloaded_filename = ?, updated_at = ? WHERE task_id = ?",
                (filename, int(time.time()), task_id),
            )

    def delete_local_job(self, task_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM jobs WHERE task_id = ?", (task_id,))

    @staticmethod
    def _submission(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["request"] = json.loads(data.pop("request_json"))
        data["error"] = json.loads(data.pop("error_json")) if data["error_json"] else None
        return data

    def begin_submission(
        self, client_request_id: str, operation: str, request: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        now = int(time.time())
        request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
        with self._connect() as db:
            # Serialize the read/transition so only one caller can reopen a
            # definitely rejected submission for a retry.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM submissions WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
            if row is None:
                db.execute(
                    """
                    INSERT INTO submissions (
                        client_request_id, operation, status, task_id,
                        request_json, error_json, created_at, updated_at
                    ) VALUES (?, ?, 'submitting', NULL, ?, NULL, ?, ?)
                    """,
                    (client_request_id, operation, request_json, now, now),
                )
                started = True
            else:
                existing = self._submission(row)
                same_intent = (
                    existing["operation"] == operation and existing["request"] == request
                )
                if existing["status"] == "rejected" and same_intent:
                    cursor = db.execute(
                        """
                        UPDATE submissions
                        SET status = 'submitting', task_id = NULL, request_json = ?,
                            error_json = NULL, updated_at = ?
                        WHERE client_request_id = ? AND status = 'rejected'
                        """,
                        (request_json, now, client_request_id),
                    )
                    started = cursor.rowcount == 1
                else:
                    started = False
            current = db.execute(
                "SELECT * FROM submissions WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
        if current is None:  # pragma: no cover - guarded by the transaction above
            raise RuntimeError("Submission ledger entry disappeared")
        return started, self._submission(current)

    def get_submission(self, client_request_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM submissions WHERE client_request_id = ?",
                (client_request_id,),
            ).fetchone()
        if not row:
            return None
        return self._submission(row)

    def finish_submission_with_job(
        self,
        client_request_id: str,
        task_id: str,
        operation: str,
        request: dict[str, Any],
        response: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit provider acceptance and additive-pool membership atomically."""
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            submission = db.execute(
                """
                UPDATE submissions
                SET status = 'submitted', task_id = ?, updated_at = ?
                WHERE client_request_id = ? AND status = 'submitting'
                """,
                (task_id, now, client_request_id),
            )
            if submission.rowcount != 1:
                raise RuntimeError("Submission was not in a committable state")
            db.execute(
                """
                INSERT INTO jobs (
                    task_id, operation, status, request_json, response_json,
                    created_at, updated_at, downloaded_filename
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, NULL)
                ON CONFLICT(task_id) DO UPDATE SET
                    operation = excluded.operation,
                    status = CASE
                        WHEN jobs.status IN (
                            'running', 'succeeded', 'failed', 'cancelled', 'deleted',
                            'unavailable'
                        ) THEN jobs.status
                        ELSE excluded.status
                    END,
                    request_json = excluded.request_json,
                    response_json = COALESCE(jobs.response_json, excluded.response_json),
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    operation,
                    json.dumps(request),
                    json.dumps(response),
                    now,
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM jobs WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - transaction guarantees the row
            raise RuntimeError("Submitted task was not added to the active pool")
        return self._job(row)

    def fail_submission(
        self, client_request_id: str, status: str, error: dict[str, Any]
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                UPDATE submissions
                SET status = ?, error_json = ?, updated_at = ?
                WHERE client_request_id = ?
                """,
                (status, json.dumps(error), int(time.time()), client_request_id),
            )
