import hashlib
import json
from pathlib import Path

from comfy_control.catalogue_import import import_grok_catalogue
from comfy_control.control_store import ControlStore


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows))


def test_grok_catalogue_import_is_complete_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "export"
    images = source / "images"
    images.mkdir(parents=True)
    linked = b"\xff\xd8\xff\xd9"
    linked_hash = hashlib.sha256(linked).hexdigest()
    orphan = b"\xff\xd8orphan\xff\xd9"
    orphan_hash = hashlib.sha256(orphan).hexdigest()
    (images / f"{linked_hash}.jpg").write_bytes(linked)
    (images / f"{orphan_hash}.jpg").write_bytes(orphan)
    write_jsonl(
        source / "assets.jsonl",
        [
            {
                "archive": "media.zip",
                "asset_id": "image-1",
                "content_hash": linked_hash,
                "source": "asset/image-1/original_image.jpg",
            },
            {
                "archive": "messages.zip",
                "asset_id": "asset-1",
                "content_hash": orphan_hash,
                "source": "asset/asset-1/original_image.jpg",
            },
        ],
    )
    write_jsonl(
        source / "media.jsonl",
        [
            {
                "archive": "media.zip",
                "create_time": 0,
                "id": "image-1",
                "link": "https://grok.example/image-1",
                "media_type": "image",
                "original_prompt": "A test wombat",
            },
            {
                "archive": "media.zip",
                "create_time": 0,
                "id": "video-1",
                "link": "https://grok.example/video-1",
                "media_type": "video",
                "original_prompt": "The wombat moves",
            },
        ],
    )
    write_jsonl(
        source / "messages.jsonl",
        [
            {
                "archive": "messages.zip",
                "conversation_id": "conversation-1",
                "conversation_title": "Wombat",
                "create_time": 1_700_000_000_000,
                "id": "message-1",
                "message": "Make the wombat fluffier",
                "model": "grok-4-auto",
                "sender": "human",
            }
        ],
    )
    database = tmp_path / "data" / "control.db"

    first = import_grok_catalogue(source, database)
    second = import_grok_catalogue(source, database)
    store = ControlStore(database)
    histories = store.histories(limit=10)
    media = store.media_library(limit=10)

    assert first == {
        "histories_created": 4,
        "linked_images": 1,
        "messages": 1,
        "unlinked_assets": 1,
        "video_metadata_only": 1,
    }
    assert second == {
        "histories_skipped": 4,
        "media_skipped": 2,
        "video_metadata_only": 1,
    }
    assert len(histories) == 4
    assert media["count"] == 2
    assert store.media_library(query="wombat")["count"] == 1
    linked_history = next(
        item for item in histories if item["id"] == "grok_media_image-1"
    )
    assert linked_history["parameters"]["asset"]["content_hash"] == linked_hash
    assert any(
        use["source_url"] == "https://grok.example/image-1"
        for item in media["data"]
        for use in (store.media_detail(int(item["asset_id"])) or {"uses": []})["uses"]
    )
    store.close()
