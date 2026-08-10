from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from check_gateway import Gateway, check_gateway


class FakeGateway:
    def __init__(self, advertised: bool = True) -> None:
        self.advertised = advertised
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, path, payload))
        if method == "GET":
            return {"data": [{"id": "provider/model"}] if self.advertised else []}
        return {"choices": [{"message": {"content": "OK", "role": "assistant"}}]}


def test_environment_rejects_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIFROST_API_KEY", "REPLACE_ME")
    monkeypatch.setenv("BIFROST_URL", "http://bifrost.example.test")

    with pytest.raises(SystemExit, match="BIFROST_API_KEY"):
        Gateway()


def test_check_gateway_requires_advertised_model() -> None:
    with pytest.raises(SystemExit, match="not advertised"):
        check_gateway(FakeGateway(advertised=False), "provider/model")  # type: ignore[arg-type]


def test_check_gateway_probes_completion() -> None:
    gateway = FakeGateway()

    check_gateway(gateway, "provider/model")  # type: ignore[arg-type]

    assert gateway.calls[1][1] == "/v1/chat/completions"
    assert gateway.calls[1][2] is not None
    assert gateway.calls[1][2]["model"] == "provider/model"
