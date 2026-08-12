from __future__ import annotations

from pathlib import Path


def media_extension(content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].lower()
    return {
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }.get(media_type, ".bin")


def image_media_type(content: bytes) -> str:
    if content.startswith(b"GIF8"):
        return "image/gif"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def media_type_from_filename(filename: str) -> str:
    return {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".mov": "video/quicktime",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".webm": "video/webm",
        ".webp": "image/webp",
    }.get(Path(filename).suffix.lower(), "application/octet-stream")


def safe_filename(filename: str, extension: str) -> str:
    name = "".join(
        character
        for character in Path(filename).name.strip()
        if character.isalnum() or character in {" ", "-", ".", "_"}
    )
    return name or f"media{extension}"
