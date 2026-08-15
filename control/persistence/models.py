from __future__ import annotations

import struct
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
class DeploymentRecord:
    id: str
    workload: str
    provider: str
    mode: str
    resource_id: str | None
    state: str
    selection_json: str
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class GenerationRequest:
    id: str
    kind: str
    model: str
    history_id: str | None
    status: str
    payload_json: str
    error: str | None
    created_at: float
    updated_at: float


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
