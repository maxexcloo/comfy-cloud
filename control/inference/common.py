from __future__ import annotations

import base64
import json
import re
import time

import httpx
from fastapi import Request

from control.service import Controller

IMAGE_ASPECT_ALIASES = {
    "landscape": "16:9",
    "portrait": "9:16",
    "square": "1:1",
}
IMAGE_ASPECT_RE = re.compile(
    r"(?<![\w:])(?:16:9|9:16|4:3|3:4|3:2|2:3|1:1|landscape|portrait|square)(?![\w:])",
    re.IGNORECASE,
)
IMAGE_RESOLUTION_RE = re.compile(r"(?<!\w)(?:1k|2k)(?!\w)", re.IGNORECASE)


def normalise_grok_image_options(values: dict[str, object]) -> dict[str, object]:
    """Fill Grok image options from explicit prompt phrases when fields are absent."""
    model = str(values.get("model") or "").rsplit("/", 1)[-1]
    if model != "grok-imagine-image-quality":
        return values
    normalised = dict(values)
    prompt = str(values.get("prompt") or "")
    aspect = str(values.get("aspect_ratio") or "").casefold().strip()
    if aspect in {"", "auto"} and (match := IMAGE_ASPECT_RE.search(prompt)):
        selected = match.group(0).casefold()
        normalised["aspect_ratio"] = IMAGE_ASPECT_ALIASES.get(selected, selected)
    resolution = str(values.get("resolution") or "").casefold().strip()
    if not resolution:
        match = IMAGE_RESOLUTION_RE.search(prompt)
        normalised["resolution"] = match.group(0).casefold() if match else "1k"
    return normalised


def archived_image_content(
    request: Request, controller: Controller, history_id: str, response: httpx.Response
) -> bytes:
    try:
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return response.content
        archived = iter(controller.store.media_for_history(history_id))
        for item in data:
            if not isinstance(item, dict):
                continue
            if not (item.get("b64_json") or item.get("url")):
                continue
            media = next(archived, None)
            if media is not None and item.get("url"):
                item["url"] = str(
                    request.url_for(
                        "history_media", history_id=history_id, media_id=media.id
                    )
                )
        return json.dumps(payload, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return response.content


def canonical_parameters(values: dict[str, object]) -> dict[str, object]:
    parameters = {
        key: value
        for key, value in values.items()
        if key not in {"model", "n", "provider", "response_format", "size"}
    }
    size = str(values.get("size", "")).strip()
    if size and size != "auto":
        try:
            width, height = size.lower().split("x", 1)
            parameters["width"] = int(width)
            parameters["height"] = int(height)
        except ValueError as exc:
            raise ValueError("size must be WIDTHxHEIGHT or auto") from exc
    for name in ("height", "length", "seed", "steps", "width"):
        if parameters.get(name) is not None:
            if isinstance(parameters[name], bool):
                raise ValueError(f"{name} must be an integer")
            parameters[name] = int(parameters[name])
    for name in ("height", "length", "steps", "width"):
        if parameters.get(name) is not None and int(parameters[name]) < 1:
            raise ValueError(f"{name} must be at least 1")
    if parameters.get("seed") is not None and int(parameters["seed"]) < 0:
        raise ValueError("seed must be at least 0")
    if parameters.get("scale") is not None:
        if isinstance(parameters["scale"], bool):
            raise ValueError("scale must be a number")
        parameters["scale"] = float(parameters["scale"])
    return parameters


def internal_image_content(
    request: Request,
    controller: Controller,
    history_id: str,
    outputs: list[tuple[bytes, str, str]],
    response_format: str,
) -> bytes:
    data = []
    for content, content_type, filename in outputs:
        media_id = controller.media.save(history_id, content, content_type, filename)
        if response_format == "url":
            data.append(
                {
                    "url": str(
                        request.url_for(
                            "history_media",
                            history_id=history_id,
                            media_id=media_id,
                        )
                    )
                }
            )
        else:
            data.append({"b64_json": base64.b64encode(content).decode()})
    return json.dumps(
        {"created": int(time.time()), "data": data}, separators=(",", ":")
    ).encode()
