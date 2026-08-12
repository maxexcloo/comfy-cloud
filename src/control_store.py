from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz.fuzz import WRatio


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


@dataclass(frozen=True)
class MediaAsset:
    id: int
    sha256: str
    content_type: str
    path: str
    size: int
    created_at: int
    width: int | None
    height: int | None
    duration: float | None


def intrinsic_media_metadata(
    path: Path, content_type: str
) -> tuple[int | None, int | None, float | None]:
    data = path.read_bytes()
    width = height = None
    duration = None
    if content_type == "image/png" and data.startswith(b"\x89PNG") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
    elif content_type == "image/gif" and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
    elif content_type == "image/jpeg":
        position = 2
        frame_markers = {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }
        while position + 9 < len(data):
            if data[position] != 0xFF:
                position += 1
                continue
            marker = data[position + 1]
            if marker in frame_markers:
                height, width = struct.unpack(">HH", data[position + 5 : position + 9])
                break
            if marker in {0xD8, 0xD9}:
                position += 2
                continue
            if position + 4 > len(data):
                break
            position += 2 + struct.unpack(">H", data[position + 2 : position + 4])[0]
    elif content_type == "image/webp" and data[12:16] == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
    if content_type.startswith("video/"):
        marker = data.find(b"mvhd")
        if marker >= 0 and marker + 32 <= len(data):
            version = data[marker + 4]
            offset = marker + (24 if version else 16)
            size = 8 if version else 4
            timescale = int.from_bytes(data[offset : offset + 4], "big")
            ticks = int.from_bytes(data[offset + 4 : offset + 4 + size], "big")
            if timescale:
                duration = ticks / timescale
    return width, height, duration


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
                CREATE TABLE IF NOT EXISTS control_configuration (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    revision INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_secrets (
                    name TEXT PRIMARY KEY,
                    encrypted_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS media_assets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sha256 TEXT NOT NULL UNIQUE,
                    content_type TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    width INTEGER,
                    height INTEGER,
                    duration REAL
                );
                CREATE TABLE IF NOT EXISTS generation_media (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
                    asset_id INTEGER NOT NULL REFERENCES media_assets(id),
                    role TEXT NOT NULL CHECK (role IN ('input', 'output')),
                    field_name TEXT,
                    position INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    source_url TEXT,
                    legacy_media_id INTEGER UNIQUE REFERENCES media(id),
                    UNIQUE (history_id, role, field_name, position)
                );
                CREATE INDEX IF NOT EXISTS generation_media_asset
                    ON generation_media (asset_id, role, history_id);
                CREATE INDEX IF NOT EXISTS generation_media_history
                    ON generation_media (history_id, role, position);
                CREATE TABLE IF NOT EXISTS generation_parameters (
                    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
                    path TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    value_type TEXT NOT NULL,
                    text_value TEXT,
                    number_value REAL,
                    boolean_value INTEGER,
                    PRIMARY KEY (history_id, path, position)
                );
                CREATE INDEX IF NOT EXISTS generation_parameters_number
                    ON generation_parameters (path, number_value, history_id);
                CREATE INDEX IF NOT EXISTS generation_parameters_text
                    ON generation_parameters (path, text_value, history_id);
                CREATE TABLE IF NOT EXISTS provider_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    status TEXT NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS provider_attempts_history
                    ON provider_attempts (history_id, id);
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                );
                """
            )
            self.connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS generation_search USING fts5(
                    history_id UNINDEXED,
                    prompt,
                    searchable,
                    tokenize = 'trigram'
                )
                """
            )
        self._apply_migrations()
        self._migrate_media_library()

    def _apply_migrations(self) -> None:
        with self.lock, self.connection:
            columns = {
                str(row["name"])
                for row in self.connection.execute("PRAGMA table_info(media_assets)")
            }
            for name, value_type in (
                ("duration", "REAL"),
                ("height", "INTEGER"),
                ("width", "INTEGER"),
            ):
                if name not in columns:
                    self.connection.execute(
                        f"ALTER TABLE media_assets ADD COLUMN {name} {value_type}"
                    )
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_migrations (version, applied_at) "
                "VALUES (1, ?)",
                (int(time.time()),),
            )
            rows = self.connection.execute(
                "SELECT id, content_type, path FROM media_assets "
                "WHERE width IS NULL AND height IS NULL AND duration IS NULL"
            ).fetchall()
            for row in rows:
                path = Path(str(row["path"]))
                if not path.is_file():
                    continue
                width, height, duration = intrinsic_media_metadata(
                    path, str(row["content_type"])
                )
                self.connection.execute(
                    "UPDATE media_assets SET width = ?, height = ?, duration = ? "
                    "WHERE id = ?",
                    (width, height, duration, int(row["id"])),
                )

    @staticmethod
    def _parameter_values(value: object, path: str = ""):
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}" if path else str(key)
                yield from ControlStore._parameter_values(item, child)
        elif isinstance(value, list):
            for item in value:
                yield from ControlStore._parameter_values(item, path)
        elif value is None or not path:
            return
        elif isinstance(value, bool):
            yield path, "boolean", None, None, int(value)
        elif isinstance(value, (int, float)):
            yield path, "number", None, float(value), None
        elif isinstance(value, str) and not value.startswith(
            "<embedded media omitted:"
        ):
            yield path, "text", value, None, None

    def _index_history(self, history_id: str, parameters_json: str) -> None:
        parameters = json.loads(parameters_json)
        values = list(self._parameter_values(parameters))
        positions: dict[str, int] = {}
        rows = []
        for path, value_type, text_value, number_value, boolean_value in values:
            position = positions.get(path, 0)
            positions[path] = position + 1
            rows.append(
                (
                    history_id,
                    path,
                    position,
                    value_type,
                    text_value,
                    number_value,
                    boolean_value,
                )
            )
        self.connection.execute(
            "DELETE FROM generation_parameters WHERE history_id = ?", (history_id,)
        )
        self.connection.executemany(
            """
            INSERT INTO generation_parameters (
                history_id, path, position, value_type, text_value, number_value,
                boolean_value
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        history = self.connection.execute(
            "SELECT model, operation, provider FROM history WHERE id = ?", (history_id,)
        ).fetchone()
        prompt = (
            str(parameters.get("prompt", "")) if isinstance(parameters, dict) else ""
        )
        searchable = " ".join(
            [
                str(history["model"]),
                str(history["operation"]),
                str(history["provider"]),
                *(str(row[4]) for row in rows if row[3] == "text" and row[4]),
            ]
        )
        self.connection.execute(
            "DELETE FROM generation_search WHERE history_id = ?", (history_id,)
        )
        self.connection.execute(
            "INSERT INTO generation_search (history_id, prompt, searchable) VALUES (?, ?, ?)",
            (history_id, prompt, searchable),
        )

    def _migrate_media_library(self) -> None:
        with self.lock, self.connection:
            rows = self.connection.execute(
                """
                SELECT media.* FROM media
                LEFT JOIN generation_media ON generation_media.legacy_media_id = media.id
                WHERE generation_media.id IS NULL
                ORDER BY media.id
                """
            ).fetchall()
            for row in rows:
                path = Path(str(row["path"]))
                if not path.is_file():
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                self.connection.execute(
                    """
                    INSERT INTO media_assets (
                        sha256, content_type, path, size, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(sha256) DO NOTHING
                    """,
                    (
                        digest,
                        str(row["content_type"]),
                        str(path),
                        int(row["size"]),
                        int(time.time()),
                    ),
                )
                asset = self.connection.execute(
                    "SELECT id FROM media_assets WHERE sha256 = ?", (digest,)
                ).fetchone()
                position = self.connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM generation_media
                    WHERE history_id = ? AND role = 'output'
                    """,
                    (str(row["history_id"]),),
                ).fetchone()["count"]
                self.connection.execute(
                    """
                    INSERT INTO generation_media (
                        history_id, asset_id, role, field_name, position, filename,
                        legacy_media_id
                    ) VALUES (?, ?, 'output', NULL, ?, ?, ?)
                    """,
                    (
                        str(row["history_id"]),
                        int(asset["id"]),
                        int(position),
                        str(row["filename"]),
                        int(row["id"]),
                    ),
                )
            unindexed = self.connection.execute(
                """
                SELECT history.id, history.parameters_json FROM history
                LEFT JOIN generation_search ON generation_search.history_id = history.id
                WHERE generation_search.history_id IS NULL
                """
            ).fetchall()
            for row in unindexed:
                self._index_history(str(row["id"]), str(row["parameters_json"]))

    def configuration(
        self,
    ) -> tuple[int, dict[str, object], dict[str, str]] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT revision, document_json FROM control_configuration WHERE id = 1"
            ).fetchone()
            if row is None:
                return None
            secret_rows = self.connection.execute(
                "SELECT name, encrypted_value FROM control_secrets ORDER BY name"
            ).fetchall()
        document = json.loads(str(row["document_json"]))
        if not isinstance(document, dict):
            raise TypeError("stored control configuration must be an object")
        return (
            int(row["revision"]),
            document,
            {str(item["name"]): str(item["encrypted_value"]) for item in secret_rows},
        )

    def save_configuration(
        self,
        document: dict[str, object],
        secrets: dict[str, str],
        *,
        expected_revision: int,
    ) -> int:
        with self.lock, self.connection:
            row = self.connection.execute(
                "SELECT revision FROM control_configuration WHERE id = 1"
            ).fetchone()
            current_revision = int(row["revision"]) if row else 0
            if current_revision != expected_revision:
                raise RuntimeError("configuration revision changed")
            revision = current_revision + 1
            self.connection.execute(
                """
                INSERT INTO control_configuration (id, revision, document_json, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    revision = excluded.revision,
                    document_json = excluded.document_json,
                    updated_at = excluded.updated_at
                """,
                (
                    revision,
                    json.dumps(document, separators=(",", ":"), sort_keys=True),
                    int(time.time()),
                ),
            )
            self.connection.execute("DELETE FROM control_secrets")
            self.connection.executemany(
                "INSERT INTO control_secrets (name, encrypted_value) VALUES (?, ?)",
                sorted(secrets.items()),
            )
        return revision

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
            attempt_rows = self.connection.execute(
                """
                SELECT id, history_id, provider, started_at, finished_at, status, error
                FROM provider_attempts
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
        attempts_by_history: dict[str, list[dict[str, object]]] = {}
        for row in attempt_rows:
            item = dict(row)
            history_id = str(item.pop("history_id"))
            attempts_by_history.setdefault(history_id, []).append(item)
        histories = []
        for row in rows:
            item = dict(row)
            item["parameters"] = json.loads(str(item.pop("parameters_json")))
            item["media"] = media_by_history.get(str(item["id"]), [])
            item["attempts"] = attempts_by_history.get(str(item["id"]), [])
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
            self._index_history(history_id, parameters_json)

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
            row = self.connection.execute(
                "SELECT parameters_json FROM history WHERE id = ?", (history_id,)
            ).fetchone()
            if row is not None:
                self._index_history(history_id, str(row["parameters_json"]))

    def start_attempt(self, history_id: str, provider: str) -> int:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "INSERT INTO provider_attempts "
                "(history_id, provider, started_at, status) "
                "VALUES (?, ?, ?, 'running')",
                (history_id, provider, int(time.time())),
            )
        return int(cursor.lastrowid)

    def finish_attempt(
        self, attempt_id: int, status: str, error: str | None = None
    ) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE provider_attempts SET finished_at = ?, status = ?, error = ? "
                "WHERE id = ?",
                (int(time.time()), status, error, attempt_id),
            )

    def attempts(self, history_id: str) -> list[dict[str, object]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT id, provider, started_at, finished_at, status, error "
                "FROM provider_attempts WHERE history_id = ? ORDER BY id",
                (history_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
            self._save_generation_media(
                history_id,
                content_type,
                filename,
                path,
                size,
                role="output",
                field_name=None,
                legacy_media_id=int(cursor.lastrowid),
            )
        return int(cursor.lastrowid)

    def _save_generation_media(
        self,
        history_id: str,
        content_type: str,
        filename: str,
        path: Path,
        size: int,
        *,
        role: str,
        field_name: str | None,
        legacy_media_id: int | None = None,
        source_url: str | None = None,
    ) -> int:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        width, height, duration = intrinsic_media_metadata(
            path, content_type.split(";", 1)[0]
        )
        self.connection.execute(
            """
            INSERT INTO media_assets (
                sha256, content_type, path, size, created_at, width, height, duration
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO NOTHING
            """,
            (
                digest,
                content_type.split(";", 1)[0],
                str(path),
                size,
                int(time.time()),
                width,
                height,
                duration,
            ),
        )
        asset = self.connection.execute(
            "SELECT id FROM media_assets WHERE sha256 = ?", (digest,)
        ).fetchone()
        position = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM generation_media
            WHERE history_id = ? AND role = ? AND field_name IS ?
            """,
            (history_id, role, field_name),
        ).fetchone()["count"]
        self.connection.execute(
            """
            INSERT INTO generation_media (
                history_id, asset_id, role, field_name, position, filename,
                source_url, legacy_media_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                history_id,
                int(asset["id"]),
                role,
                field_name,
                int(position),
                filename,
                source_url,
                legacy_media_id,
            ),
        )
        return int(asset["id"])

    def save_input_media(
        self,
        history_id: str,
        content_type: str,
        filename: str,
        path: Path,
        size: int,
        *,
        field_name: str,
        source_url: str | None = None,
    ) -> int:
        with self.lock, self.connection:
            return self._save_generation_media(
                history_id,
                content_type,
                filename,
                path,
                size,
                role="input",
                field_name=field_name,
                source_url=source_url,
            )

    def media_asset(self, asset_id: int) -> MediaAsset | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT * FROM media_assets WHERE id = ?", (asset_id,)
            ).fetchone()
        return MediaAsset(**dict(row)) if row else None

    def media_library(
        self,
        *,
        query: str = "",
        filters: list[dict[str, object]] | None = None,
        include_inputs: bool = False,
        sort: str = "newest",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        filters = filters or []
        clauses = ["(? OR generation_media.role = 'output')"]
        values: list[object] = [int(include_inputs)]
        terms = [term for term in query.strip().split() if len(term) >= 3]
        if terms:
            escaped_terms = [
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
            ]
            clauses.append("generation_search MATCH ?")
            values.append(" OR ".join(escaped_terms))
        for item in filters:
            path = str(item.get("path", ""))
            operator = str(item.get("operator", "equals"))
            value = item.get("value")
            if path in {"model", "operation", "provider", "status"}:
                column = f"history.{path}"
                if operator == "equals":
                    clauses.append(f"{column} = ?")
                    values.append(value)
                elif operator == "not_equals":
                    clauses.append(f"{column} != ?")
                    values.append(value)
                continue
            parameter_column = (
                "number_value" if isinstance(value, (int, float)) else "text_value"
            )
            comparison = {
                "contains": "LIKE",
                "equals": "=",
                "greater_than": ">",
                "less_than": "<",
                "not_equals": "!=",
            }.get(operator, "=")
            clauses.append(
                "EXISTS (SELECT 1 FROM generation_parameters parameter "
                "WHERE parameter.history_id = history.id AND parameter.path = ? "
                f"AND parameter.{parameter_column} {comparison} ?)"
            )
            values.extend([path, f"%{value}%" if comparison == "LIKE" else value])
        with self.lock:
            rows = self.connection.execute(
                f"""
            SELECT
                media_assets.id AS asset_id,
                media_assets.content_type,
                media_assets.duration,
                media_assets.height,
                media_assets.size,
                media_assets.width,
                generation_media.filename,
                generation_media.field_name,
                generation_media.role,
                generation_media.position,
                history.id AS history_id,
                history.operation,
                history.model,
                history.provider,
                history.status,
                history.created_at,
                history.parameters_json,
                generation_search.prompt,
                generation_search.searchable
            FROM generation_media
            JOIN media_assets ON media_assets.id = generation_media.asset_id
            JOIN history ON history.id = generation_media.history_id
            JOIN generation_search ON generation_search.history_id = history.id
            WHERE {" AND ".join(clauses)}
            ORDER BY history.created_at DESC, generation_media.id DESC
            LIMIT 5000
                """,
                values,
            ).fetchall()
        items = [dict(row) for row in rows]
        if query.strip():
            needle = query.strip()
            for item in items:
                item["relevance"] = WRatio(
                    needle, f"{item['prompt']} {item['searchable']}"
                )
            items = [item for item in items if int(item["relevance"]) >= 35]
            items.sort(
                key=lambda item: (int(item["relevance"]), int(item["created_at"])),
                reverse=True,
            )
        elif sort == "oldest":
            items.sort(
                key=lambda item: (int(item["created_at"]), int(item["asset_id"]))
            )
        elif sort in {"model", "provider", "content_type"}:
            items.sort(
                key=lambda item: (
                    str(item[sort]),
                    -int(item["created_at"]),
                    int(item["asset_id"]),
                )
            )
        elif sort.startswith("parameter:"):
            sort_path = sort.removeprefix("parameter:")

            def parameter_value(
                item: dict[str, object],
            ) -> tuple[bool, int, float | str]:
                value: object = json.loads(str(item["parameters_json"]))
                for part in sort_path.split("."):
                    if not isinstance(value, dict) or part not in value:
                        return True, 2, ""
                    value = value[part]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return False, 0, float(value)
                return value is None, 1, "" if value is None else str(value)

            items.sort(key=lambda item: (*parameter_value(item), int(item["asset_id"])))
        for item in items:
            item.pop("searchable", None)
            item["parameters"] = json.loads(str(item.pop("parameters_json")))
        return {"count": len(items), "data": items[offset : offset + limit]}

    def media_detail(self, asset_id: int) -> dict[str, object] | None:
        with self.lock:
            asset = self.connection.execute(
                "SELECT * FROM media_assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if asset is None:
                return None
            uses = self.connection.execute(
                """
                SELECT generation_media.history_id, generation_media.role,
                       generation_media.field_name, generation_media.filename,
                       history.model, history.operation, history.provider,
                       history.status, history.created_at, history.parameters_json
                FROM generation_media
                JOIN history ON history.id = generation_media.history_id
                WHERE generation_media.asset_id = ?
                ORDER BY history.created_at DESC, generation_media.position
                """,
                (asset_id,),
            ).fetchall()
        detail = dict(asset)
        detail["uses"] = []
        for row in uses:
            use = dict(row)
            use["parameters"] = json.loads(str(use.pop("parameters_json")))
            detail["uses"].append(use)
        detail["lineage"] = self.media_lineage(asset_id)
        return detail

    def media_lineage(self, asset_id: int) -> dict[str, list[dict[str, object]]]:
        with self.lock:
            sources = self.connection.execute(
                """
                SELECT DISTINCT asset.id, asset.content_type, media.filename,
                       media.history_id
                FROM generation_media selected
                JOIN generation_media media
                  ON media.history_id = selected.history_id AND media.role = 'input'
                JOIN media_assets asset ON asset.id = media.asset_id
                WHERE selected.asset_id = ? AND selected.role = 'output'
                ORDER BY media.position
                """,
                (asset_id,),
            ).fetchall()
            derivatives = self.connection.execute(
                """
                SELECT DISTINCT asset.id, asset.content_type, media.filename,
                       media.history_id
                FROM generation_media selected
                JOIN generation_media media
                  ON media.history_id = selected.history_id AND media.role = 'output'
                JOIN media_assets asset ON asset.id = media.asset_id
                WHERE selected.asset_id = ? AND selected.role = 'input'
                ORDER BY media.position
                """,
                (asset_id,),
            ).fetchall()
        return {
            "derivatives": [dict(row) for row in derivatives],
            "sources": [dict(row) for row in sources],
        }

    def media_facets(self) -> dict[str, list[object]]:
        with self.lock:
            values = {
                name: [
                    row["value"]
                    for row in self.connection.execute(
                        f"SELECT DISTINCT {name} AS value FROM history "
                        f"WHERE {name} != '' ORDER BY {name}"
                    ).fetchall()
                ]
                for name in ("model", "operation", "provider", "status")
            }
            values["parameters"] = [
                row["path"]
                for row in self.connection.execute(
                    "SELECT DISTINCT path FROM generation_parameters ORDER BY path"
                ).fetchall()
            ]
        return values

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
