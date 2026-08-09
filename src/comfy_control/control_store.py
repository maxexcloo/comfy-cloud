from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Job:
    id: str
    model: str
    provider: str
    status: str
    upstream_id: str
    created_at: int
    updated_at: int
    request_json: str
    response_json: str | None
    error: str | None


class ControlStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.connection:
            self.connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    provider TEXT,
                    request_id TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    upstream_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    request_json TEXT NOT NULL,
                    response_json TEXT,
                    error TEXT
                );
                """
            )

    def close(self) -> None:
        self.connection.close()

    def event(
        self,
        level: str,
        message: str,
        *,
        provider: str | None = None,
        request_id: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO events (created_at, level, message, provider, request_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(time.time()), level, message, provider, request_id),
            )
            self.connection.execute(
                """
                DELETE FROM events
                WHERE id <= (SELECT COALESCE(MAX(id), 0) - 2000 FROM events)
                """
            )

    def events(self, limit: int = 100) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT created_at, level, message, provider, request_id
                FROM events ORDER BY id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def job(self, job_id: str) -> Job | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return Job(**dict(row)) if row else None

    def jobs(self, limit: int = 100) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT id, model, provider, status, created_at, updated_at, error
                FROM jobs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_job(
        self,
        job_id: str,
        model: str,
        request_json: str,
    ) -> None:
        now = int(time.time())
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO jobs (
                    id, model, provider, status, upstream_id, created_at, updated_at,
                    request_json, response_json, error
                ) VALUES (?, ?, '', 'queued', '', ?, ?, ?, NULL, NULL)
                ON CONFLICT(id) DO NOTHING
                """,
                (job_id, model, now, now, request_json),
            )

    def update_job(
        self,
        job_id: str,
        status: str,
        *,
        provider: str | None = None,
        upstream_id: str | None = None,
        response_json: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE jobs SET
                    status = ?,
                    provider = COALESCE(?, provider),
                    upstream_id = COALESCE(?, upstream_id),
                    response_json = COALESCE(?, response_json),
                    request_json = CASE
                        WHEN ? IN ('cancelled', 'completed', 'failed') THEN '{}'
                        ELSE request_json
                    END,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    provider,
                    upstream_id,
                    response_json,
                    status,
                    error,
                    int(time.time()),
                    job_id,
                ),
            )

    def pending_jobs(self) -> list[Job]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'in_progress')
                ORDER BY created_at
                """
            ).fetchall()
        return [Job(**dict(row)) for row in rows]
