import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfy_cloud.fetch import fetch_profile


def test_huggingface_destination_places_root_file_in_model_subdirectory(
    monkeypatch, tmp_path
):
    def snapshot_download(**kwargs):
        root = Path(kwargs["local_dir"])
        (root / "model.safetensors").write_bytes(b"model")
        return str(root)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        """
sources:
  - type: huggingface
    repo: owner/model
    revision: immutable-revision
    include: [model.safetensors]
    destination: diffusion_models
""".strip()
    )

    downloaded = fetch_profile(profile, tmp_path / "models")

    assert downloaded == [tmp_path / "models/diffusion_models/model.safetensors"]
    assert downloaded[0].read_bytes() == b"model"


def test_prepared_profile_is_not_downloaded_again(monkeypatch, tmp_path):
    calls = 0

    def snapshot_download(**kwargs):
        nonlocal calls
        calls += 1
        root = Path(kwargs["local_dir"])
        (root / "model.safetensors").write_bytes(b"model")
        return str(root)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(snapshot_download=snapshot_download),
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        """
name: cached-profile
sources:
  - type: huggingface
    repo: owner/model
    revision: immutable-revision
    include: [model.safetensors]
    destination: diffusion_models
""".strip()
    )
    models = tmp_path / "models"

    first = fetch_profile(profile, models)
    second = fetch_profile(profile, models)

    assert first == second
    assert calls == 1


def test_civitai_destination_cannot_escape_models_directory(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        """
sources:
  - type: civitai
    version_id: 123
    destination: ../outside/model.safetensors
""".strip()
    )

    with pytest.raises(ValueError, match="Civitai destination"):
        fetch_profile(profile, tmp_path / "models")
