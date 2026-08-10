from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value == "REPLACE_ME":
        raise SystemExit(f"set {name} in .env")
    return value


class Gateway:
    def __init__(self, base_url: str = "http://localhost:28080") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = required_environment("BIFROST_API_KEY")

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urlopen(request, timeout=60) as response:
            return json.load(response)


def check_gateway(gateway: Gateway, model: str, operation: str) -> None:
    response = gateway.request("GET", "/v1/models")
    models = response.get("data")
    if not isinstance(models, list) or model not in {
        item.get("id") for item in models if isinstance(item, dict)
    }:
        raise SystemExit(f"model is not advertised: {model}")

    if operation == "image_generation":
        response = gateway.request(
            "POST",
            "/v1/images/generations",
            {
                "model": model,
                "n": 1,
                "prompt": "A small red circle on a white background.",
                "response_format": "b64_json",
            },
        )
        images = response.get("data")
        if (
            not isinstance(images, list)
            or not images
            or not isinstance(images[0], dict)
            or not images[0].get("b64_json")
        ):
            raise SystemExit("gateway returned no image data")
        return

    response = gateway.request(
        "POST",
        "/v1/chat/completions",
        {
            "max_tokens": 8,
            "messages": [{"content": "Reply with OK.", "role": "user"}],
            "model": model,
            "stream": False,
        },
    )
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise SystemExit("gateway returned no completion choices")


def run(model: str, base_url: str, operation: str) -> None:
    gateway = Gateway(base_url)
    try:
        check_gateway(gateway, model, operation)
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        message = f"Bifrost returned HTTP {exc.code}"
        if detail:
            message = f"{message}: {detail}"
        raise SystemExit(message) from exc
    except URLError as exc:
        raise SystemExit(f"could not reach Bifrost: {exc.reason}") from exc
    print(f"healthy: {model} ({operation})")
    print(f"Bifrost: {gateway.base_url}")
