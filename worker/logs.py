from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from subprocess import Popen
from typing import IO

DEFAULT_LOG_PATH = "/tmp/comfy-control-worker.jsonl"
MAXIMUM_LOG_BYTES = 2 * 1024 * 1024
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def log_path() -> Path:
    return Path(os.getenv("WORKER_LOG_PATH", DEFAULT_LOG_PATH))


def _level(message: str) -> str:
    upper = message.upper()
    if "ERROR" in upper or "TRACEBACK" in upper or "EXCEPTION" in upper:
        return "error"
    if "WARNING" in upper or "WARN" in upper:
        return "warning"
    return "info"


def _record(output: IO[str], message: str) -> None:
    message = ANSI_ESCAPE.sub("", message)
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size >= MAXIMUM_LOG_BYTES:
        path.write_text("")
    entry = {
        "created_at": int(time.time()),
        "level": _level(message),
        "message": message,
        "source": "Worker",
    }
    with path.open("a") as destination:
        destination.write(json.dumps(entry, separators=(",", ":")) + "\n")
    print(message, file=output, flush=True)


def capture_process_logs(process: Popen[str], output: IO[str]) -> threading.Thread:
    def capture() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            _record(output, line.rstrip("\n"))

    thread = threading.Thread(target=capture, daemon=True, name="comfyui-logs")
    thread.start()
    return thread


def entries(limit: int) -> list[dict[str, object]]:
    path = log_path()
    if not path.is_file():
        return []
    values: list[dict[str, object]] = []
    for line in _tail_lines(path, limit):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _tail_lines(path: Path, limit: int) -> Iterator[str]:
    lines = path.read_text(errors="replace").splitlines()
    yield from lines[-max(limit, 1) :]
