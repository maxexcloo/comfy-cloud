from __future__ import annotations

import hashlib
import json
from pathlib import Path

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024 * 1024


def pack_file(
    source: Path, destination: Path, chunk_size: int = DEFAULT_CHUNK_SIZE
) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    chunks: list[dict] = []
    with source.open("rb") as handle:
        index = 0
        while True:
            chunk_path = destination / f"{source.name}.part-{index:05d}"
            chunk_digest = hashlib.sha256()
            size = 0
            with chunk_path.open("wb") as chunk:
                while size < chunk_size:
                    block = handle.read(min(8 * 1024 * 1024, chunk_size - size))
                    if not block:
                        break
                    chunk.write(block)
                    chunk_digest.update(block)
                    digest.update(block)
                    size += len(block)
            if size == 0:
                chunk_path.unlink()
                break
            chunks.append(
                {
                    "file": chunk_path.name,
                    "size": size,
                    "sha256": chunk_digest.hexdigest(),
                }
            )
            index += 1
    manifest = {
        "name": source.name,
        "size": source.stat().st_size,
        "sha256": digest.hexdigest(),
        "chunks": chunks,
    }
    (destination / f"{source.name}.pack.json").write_text(
        json.dumps(manifest, indent=2)
    )
    return manifest


def unpack_file(manifest_path: Path, destination: Path) -> Path:
    manifest = json.loads(manifest_path.read_text())
    destination.mkdir(parents=True, exist_ok=True)
    output = destination / manifest["name"]
    digest = hashlib.sha256()
    with output.open("wb") as target:
        for item in manifest["chunks"]:
            chunk = manifest_path.parent / item["file"]
            chunk_digest = hashlib.sha256()
            with chunk.open("rb") as source:
                while block := source.read(8 * 1024 * 1024):
                    target.write(block)
                    digest.update(block)
                    chunk_digest.update(block)
            if chunk_digest.hexdigest() != item["sha256"]:
                output.unlink(missing_ok=True)
                raise ValueError(f"chunk checksum mismatch: {chunk}")
    if (
        output.stat().st_size != manifest["size"]
        or digest.hexdigest() != manifest["sha256"]
    ):
        output.unlink(missing_ok=True)
        raise ValueError("reconstructed model checksum mismatch")
    return output
