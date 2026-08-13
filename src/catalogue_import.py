from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .control_store import ControlStore


def jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise TypeError(f"JSON record at {path}:{line_number} is not an object")
            rows.append(value)
    return rows


def parameters(
    kind: str,
    exported_at: int,
    record: dict[str, Any],
    *,
    asset: dict[str, Any] | None = None,
) -> str:
    value: dict[str, Any] = {
        "import": {
            "exported_at": exported_at,
            "kind": kind,
            "source": "Grok catalogue export",
        },
        "source": record,
    }
    if prompt := record.get("original_prompt"):
        value["prompt"] = prompt
    if asset is not None:
        value["asset"] = asset
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def copy_asset(
    asset: dict[str, Any], images: Path, media_root: Path, history_id: str
) -> Path:
    digest = str(asset["content_hash"])
    source = images / f"{digest}.jpg"
    if not source.is_file():
        raise FileNotFoundError(f"catalogue asset is missing: {source}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError(f"catalogue asset hash does not match: {source}")
    destination = media_root / history_id / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file():
        temporary = destination.with_suffix(".jpg.part")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    return destination


def import_grok_catalogue(source: Path, database: Path) -> dict[str, int]:
    assets = jsonl(source / "assets.jsonl")
    media = jsonl(source / "media.jsonl")
    messages = jsonl(source / "messages.jsonl")
    images = source / "images"
    exported_at = int(
        min(
            (source / name).stat().st_mtime
            for name in ("assets.jsonl", "media.jsonl", "messages.jsonl")
        )
    )
    assets_by_id = {str(item["asset_id"]): item for item in assets}
    media_ids = {str(item["id"]) for item in media}
    media_root = database.parent / "media"
    store = ControlStore(database)
    counts: Counter[str] = Counter()
    try:
        for item in media:
            media_id = str(item["id"])
            media_type = str(item["media_type"])
            history_id = f"grok_media_{media_id}"
            operation = (
                "image_generation" if media_type == "image" else "video_generation"
            )
            asset = assets_by_id.get(media_id)
            if store.save_imported_history(
                history_id,
                operation,
                "grok-imagine",
                parameters("generated_media", exported_at, item, asset=asset),
                created_at=exported_at,
                provider="grok",
                provider_model="grok-imagine",
            ):
                counts["histories_created"] += 1
            else:
                counts["histories_skipped"] += 1
            if asset is None:
                counts[f"{media_type}_metadata_only"] += 1
                continue
            if store.media_for_history(history_id):
                counts["media_skipped"] += 1
                continue
            path = copy_asset(asset, images, media_root, history_id)
            store.save_media(
                history_id,
                "image/jpeg",
                path.name,
                path,
                path.stat().st_size,
                source_url=str(item.get("link") or "") or None,
            )
            counts["linked_images"] += 1

        for asset_id in sorted(assets_by_id.keys() - media_ids):
            asset = assets_by_id[asset_id]
            history_id = f"grok_asset_{asset_id}"
            if store.save_imported_history(
                history_id,
                "asset_import",
                "chat-attachment",
                parameters("unlinked_asset", exported_at, asset),
                created_at=exported_at,
                provider="grok",
                provider_model="chat-attachment",
            ):
                counts["histories_created"] += 1
            else:
                counts["histories_skipped"] += 1
            if store.media_for_history(history_id):
                counts["media_skipped"] += 1
                continue
            path = copy_asset(asset, images, media_root, history_id)
            store.save_media(
                history_id, "image/jpeg", path.name, path, path.stat().st_size
            )
            counts["unlinked_assets"] += 1

        for item in messages:
            message_id = str(item["id"])
            created_at = int(item.get("create_time") or exported_at * 1000) // 1000
            message_parameters = {
                "import": {
                    "exported_at": exported_at,
                    "kind": "conversation_message",
                    "source": "Grok catalogue export",
                },
                "source": item,
            }
            if store.save_imported_history(
                f"grok_message_{message_id}",
                "conversation_message",
                str(item.get("model") or "grok"),
                json.dumps(message_parameters, separators=(",", ":"), sort_keys=True),
                created_at=created_at,
                provider="grok",
                provider_model=str(item.get("model") or "grok"),
            ):
                counts["histories_created"] += 1
                counts["messages"] += 1
            else:
                counts["histories_skipped"] += 1
    finally:
        store.close()
    return dict(sorted(counts.items()))
