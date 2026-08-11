from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Media:
    id: int
    history_id: str
    content_type: str
    filename: str
    path: str
    size: int


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
                CREATE TABLE IF NOT EXISTS history (
                    id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    parameters_json TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_history_id
                    ON media (history_id, id);
                CREATE TABLE IF NOT EXISTS provider_resources (
                    provider TEXT PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )

    def histories(self, limit: int = 100, offset: int = 0) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT id, operation, model, provider, status, created_at, updated_at,
                       parameters_json, error
                FROM history ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            media_rows = self.connection.execute(
                """
                SELECT id, history_id, content_type, filename, size
                FROM media
                WHERE history_id IN (
                    SELECT id FROM history
                    ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
                )
                ORDER BY id
                """,
                (limit, offset),
            ).fetchall()
        media_by_history: dict[str, list[dict[str, object]]] = {}
        for row in media_rows:
            item = dict(row)
            history_id = str(item.pop("history_id"))
            media_by_history.setdefault(history_id, []).append(item)
        histories = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(str(item.pop("parameters_json")))
            item["media"] = media_by_history.get(str(item["id"]), [])
            histories.append(item)
        return histories

    def history_count(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM history"
            ).fetchone()
        return int(row["count"])

    def history_usage(self, provider: str) -> dict[str, int]:
        with self.lock:
            row = self.connection.execute(
                """
                SELECT
                    COUNT(*) AS total_requests,
                    COALESCE(SUM(status = 'failed'), 0) AS failed_requests,
                    COALESCE(SUM(status = 'completed'), 0) AS successful_requests
                FROM history WHERE provider = ?
                """,
                (provider,),
            ).fetchone()
        return {
            "failed_requests": int(row["failed_requests"]),
            "successful_requests": int(row["successful_requests"]),
            "total_requests": int(row["total_requests"]),
        }

    def media(self, media_id: int) -> Media | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM media WHERE id = ?", (media_id,)
            ).fetchone()
        return Media(**dict(row)) if row else None

    def media_for_history(self, history_id: str) -> list[Media]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM media WHERE history_id = ? ORDER BY id",
                (history_id,),
            ).fetchall()
        return [Media(**dict(row)) for row in rows]

    def save_history(
        self,
        history_id: str,
        operation: str,
        model: str,
        parameters_json: str,
    ) -> None:
        now = int(time.time())
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO history (
                    id, operation, model, provider, status, created_at, updated_at,
                    parameters_json, error
                ) VALUES (?, ?, ?, '', 'queued', ?, ?, ?, NULL)
                ON CONFLICT(id) DO NOTHING
                """,
                (history_id, operation, model, now, now, parameters_json),
            )

    def update_history(
        self,
        history_id: str,
        status: str,
        *,
        provider: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                UPDATE history SET
                    provider = COALESCE(?, provider),
                    status = ?,
                    updated_at = ?,
                    error = ?
                WHERE id = ?
                """,
                (provider, status, int(time.time()), error, history_id),
            )

    def save_media(
        self,
        history_id: str,
        content_type: str,
        filename: str,
        path: Path,
        size: int,
    ) -> int:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO media (
                    history_id, content_type, filename, path, size
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (history_id, content_type, filename, str(path), size),
            )
        return int(cursor.lastrowid)

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

    def event_count(self) -> int:
        with self.lock:
            row = self.connection.execute(
                "SELECT COUNT(*) AS count FROM events"
            ).fetchone()
        return int(row["count"])

    def events(self, limit: int = 100, offset: int = 0) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT created_at, level, message, provider, request_id
                FROM events ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def provider_resource(self, provider: str) -> str | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT resource_id FROM provider_resources WHERE provider = ?",
                (provider,),
            ).fetchone()
        return str(row["resource_id"]) if row else None

    def save_provider_resource(self, provider: str, resource_id: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO provider_resources (provider, resource_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    resource_id = excluded.resource_id,
                    updated_at = excluded.updated_at
                """,
                (provider, resource_id, int(time.time())),
            )

    def clear_provider_resource(self, provider: str) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "DELETE FROM provider_resources WHERE provider = ?", (provider,)
            )

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
        provider: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO jobs (
                    id, model, provider, status, upstream_id, created_at, updated_at,
                    request_json, response_json, error
                ) VALUES (?, ?, ?, 'queued', '', ?, ?, ?, NULL, NULL)
                ON CONFLICT(id) DO NOTHING
                """,
                (job_id, model, provider or "", now, now, request_json),
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
