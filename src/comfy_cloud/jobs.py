from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JobStore:
    """File-backed video job store.

    Each job is a JSON file named ``<job_id>.json`` in ``jobs_dir``. Jobs are
    written on every transition and reloaded on startup, so a worker that
    restarts keeps its completed or in-progress job records. ComfyUI's output
    files are not moved here; completed jobs that reference worker-local output
    remain readable only while the worker that produced them is alive.
    """

    def __init__(self, directory: Path | None):
        self.directory = directory
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[dict[str, Any]]:
        if self.directory is None:
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return records

    def save(self, record: dict[str, Any]) -> None:
        if self.directory is None:
            return
        path = self.directory / f"{record['id']}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record))
        temporary.replace(path)

    def delete(self, job_id: str) -> None:
        if self.directory is None:
            return
        (self.directory / f"{job_id}.json").unlink(missing_ok=True)
