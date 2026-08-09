from pathlib import Path

import yaml

from comfy_control.supervisor import _comfy_arguments, _prepare_models


def test_external_models_directory_is_added_to_comfy(monkeypatch, tmp_path):
    comfy_dir = tmp_path / "ComfyUI"
    models_dir = tmp_path / "models"
    comfy_dir.mkdir()
    models_dir.mkdir()
    monkeypatch.setenv("MODELS_DIR", str(models_dir))

    arguments = _comfy_arguments(comfy_dir)
    config_path = Path(arguments[arguments.index("--extra-model-paths-config") + 1])
    config = yaml.safe_load(config_path.read_text())

    assert config["comfy_control"]["base_path"] == str(models_dir)
    assert config["comfy_control"]["diffusion_models"] == "diffusion_models"


def test_configured_model_profile_is_prepared(monkeypatch, tmp_path):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    profile = profiles_dir / "test-profile.yaml"
    profile.write_text("name: test-profile\nsources: []\n")
    calls = []
    monkeypatch.setenv("MODEL_PROFILES", "test-profile")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("PROFILES_DIR", str(profiles_dir))
    monkeypatch.setattr(
        "comfy_control.supervisor.fetch_profile",
        lambda profile_path, models_dir: calls.append((profile_path, models_dir)) or [],
    )

    _prepare_models()

    assert calls == [(profile, tmp_path / "models")]
