from __future__ import annotations

from typing import Any

import pytest

from comfy_control.gateway_check import Gateway, check_gateway


class FakeGateway:
    def __init__(self, advertised: bool = True, image: bool = False) -> None:
        self.advertised = advertised
        self.image = image
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, payload))
        if method == "GET":
            return {"data": [{"id": "provider/model"}] if self.advertised else []}
        if self.image:
            return {"data": [{"b64_json": "aW1hZ2U="}]}
        return {"choices": [{"message": {"content": "OK", "role": "assistant"}}]}


def test_environment_rejects_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_API_KEY", "REPLACE_ME")

    with pytest.raises(SystemExit, match="BIFROST_API_KEY"):
        Gateway()


def test_check_gateway_requires_advertised_model() -> None:
    with pytest.raises(SystemExit, match="not advertised"):
        check_gateway(
            FakeGateway(advertised=False),  # type: ignore[arg-type]
            "provider/model",
            "chat_completion",
        )


def test_check_gateway_probes_completion() -> None:
    gateway = FakeGateway()

    check_gateway(
        gateway,
        "provider/model",
        "chat_completion",  # type: ignore[arg-type]
    )

    assert gateway.calls[1][1] == "/v1/chat/completions"
    assert gateway.calls[1][2] is not None
    assert gateway.calls[1][2]["model"] == "provider/model"


def test_check_gateway_probes_image_generation() -> None:
    gateway = FakeGateway(image=True)

    check_gateway(
        gateway,
        "provider/model",
        "image_generation",  # type: ignore[arg-type]
    )

    assert gateway.calls[1][1] == "/v1/images/generations"
    assert gateway.calls[1][2] is not None
    assert gateway.calls[1][2]["response_format"] == "b64_json"
