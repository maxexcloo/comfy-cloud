from __future__ import annotations

import sqlite3

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at INTEGER NOT NULL,
    level TEXT NOT NULL, message TEXT NOT NULL, provider TEXT, request_id TEXT
);
CREATE INDEX IF NOT EXISTS events_level ON events (level, id DESC);
CREATE INDEX IF NOT EXISTS events_provider ON events (provider, id DESC);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, model TEXT NOT NULL, provider TEXT NOT NULL,
    status TEXT NOT NULL, upstream_id TEXT NOT NULL, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, request_json TEXT NOT NULL,
    response_json TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS history (
    id TEXT PRIMARY KEY, operation TEXT NOT NULL, model TEXT NOT NULL,
    provider TEXT NOT NULL, provider_model TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    parameters_json TEXT NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS history_operation ON history (operation, created_at DESC);
CREATE INDEX IF NOT EXISTS history_provider ON history (provider, created_at DESC);
CREATE INDEX IF NOT EXISTS history_status ON history (status, created_at DESC);
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL, filename TEXT NOT NULL, path TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS media_history_id ON media (history_id, id);
CREATE TABLE IF NOT EXISTS provider_resources (
    provider TEXT PRIMARY KEY, resource_id TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY, workload TEXT NOT NULL, provider TEXT NOT NULL,
    mode TEXT NOT NULL, resource_id TEXT, state TEXT NOT NULL,
    selection_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE (workload, provider, mode)
);
CREATE INDEX IF NOT EXISTS deployments_workload
    ON deployments (workload, state, updated_at DESC);
CREATE TABLE IF NOT EXISTS deployment_benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id TEXT REFERENCES deployments(id) ON DELETE SET NULL,
    workload TEXT NOT NULL, provider TEXT NOT NULL, mode TEXT NOT NULL,
    gpu TEXT NOT NULL, worker_image TEXT NOT NULL, model_revision TEXT NOT NULL,
    sample_count INTEGER NOT NULL, p50_seconds REAL NOT NULL,
    p95_seconds REAL NOT NULL, cost_per_hour REAL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS deployment_benchmarks_lookup
    ON deployment_benchmarks (workload, provider, mode, gpu, created_at DESC);
CREATE TABLE IF NOT EXISTS generation_phases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
    phase TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS generation_phases_history
    ON generation_phases (history_id, id);
CREATE TABLE IF NOT EXISTS generation_requests (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, model TEXT NOT NULL, history_id TEXT,
    status TEXT NOT NULL, payload_json TEXT NOT NULL, error TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS generation_requests_status
    ON generation_requests (status, created_at);
CREATE TABLE IF NOT EXISTS control_configuration (
    id INTEGER PRIMARY KEY CHECK (id = 1), revision INTEGER NOT NULL,
    document_json TEXT NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS control_secrets (
    name TEXT PRIMARY KEY, encrypted_value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
    created_at INTEGER NOT NULL, width INTEGER, height INTEGER, duration REAL
);
CREATE TABLE IF NOT EXISTS generation_media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES media_assets(id),
    role TEXT NOT NULL CHECK (role IN ('input', 'output')), field_name TEXT,
    position INTEGER NOT NULL, filename TEXT NOT NULL, source_url TEXT,
    legacy_media_id INTEGER UNIQUE REFERENCES media(id),
    UNIQUE (history_id, role, field_name, position)
);
CREATE INDEX IF NOT EXISTS generation_media_asset
    ON generation_media (asset_id, role, history_id);
CREATE INDEX IF NOT EXISTS generation_media_history
    ON generation_media (history_id, role, position);
CREATE TABLE IF NOT EXISTS generation_parameters (
    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
    path TEXT NOT NULL, position INTEGER NOT NULL, value_type TEXT NOT NULL,
    text_value TEXT, number_value REAL, boolean_value INTEGER,
    PRIMARY KEY (history_id, path, position)
);
CREATE INDEX IF NOT EXISTS generation_parameters_number
    ON generation_parameters (path, number_value, history_id);
CREATE INDEX IF NOT EXISTS generation_parameters_text
    ON generation_parameters (path, text_value, history_id);
CREATE TABLE IF NOT EXISTS provider_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id TEXT NOT NULL REFERENCES history(id) ON DELETE CASCADE,
    provider TEXT NOT NULL, model TEXT NOT NULL DEFAULT '', started_at REAL NOT NULL,
    finished_at REAL, status TEXT NOT NULL, error TEXT
);
CREATE INDEX IF NOT EXISTS provider_attempts_history
    ON provider_attempts (history_id, id);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL
);
"""


def initialise_schema(connection: sqlite3.Connection) -> None:
    with connection:
        connection.executescript(SCHEMA)
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS generation_search USING fts5(
                history_id UNINDEXED, prompt, searchable, tokenize = 'trigram'
            )
            """
        )
