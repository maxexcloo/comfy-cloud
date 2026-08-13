from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import yaml

from comfy_control.supervisor import _comfy_arguments, _prepare_models
from comfy_control.worker_logs import capture_process_logs, entries


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


def test_empty_model_selection_prunes_all_managed_profiles(monkeypatch, tmp_path):
    calls = []
    models = tmp_path / "models"
    monkeypatch.setenv("MODEL_PROFILES", "")
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setattr(
        "comfy_control.supervisor.prune_profiles",
        lambda selected, models_dir: calls.append((selected, models_dir)) or [],
    )

    _prepare_models()

    assert calls == [(set(), models)]


def test_comfyui_output_is_captured_as_worker_logs(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_LOG_PATH", str(tmp_path / "worker.jsonl"))
    output = StringIO()
    process = SimpleNamespace(
        stdout=StringIO("\x1b[31mERROR failed to compile\x1b[0m\nINFO ready\n")
    )

    thread = capture_process_logs(process, output)
    thread.join(timeout=1)
    captured = entries(10)

    assert captured == [
        {
            "created_at": captured[0]["created_at"],
            "level": "error",
            "message": "ERROR failed to compile",
            "source": "Worker",
        },
        {
            "created_at": captured[1]["created_at"],
            "level": "info",
            "message": "INFO ready",
            "source": "Worker",
        },
    ]
    assert output.getvalue() == "ERROR failed to compile\nINFO ready\n"
