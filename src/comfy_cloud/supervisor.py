from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from .fetch import fetch_profile

MODEL_DIRECTORY_KEYS = ("checkpoints", "diffusion_models", "text_encoders", "vae")


def _prepare_models() -> None:
    profiles = [
        profile.strip()
        for profile in os.getenv("MODEL_PROFILES", "").split(",")
        if profile.strip()
    ]
    if not profiles:
        return
    models_dir = Path(os.getenv("MODELS_DIR", "/opt/ComfyUI/models"))
    profiles_dir = Path(os.getenv("PROFILES_DIR", "/opt/comfy-cloud/profiles"))
    for profile in profiles:
        if any(
            character not in "-0123456789_abcdefghijklmnopqrstuvwxyz"
            for character in profile
        ):
            raise ValueError(f"invalid model profile: {profile}")
        profile_path = profiles_dir / f"{profile}.yaml"
        if not profile_path.is_file():
            raise ValueError(f"unknown model profile: {profile}")
        print(f"Preparing model profile {profile}", flush=True)
        files = fetch_profile(profile_path, models_dir)
        print(f"Prepared model profile {profile} ({len(files)} files)", flush=True)


def _comfy_arguments(comfy_dir: Path) -> list[str]:
    arguments = shlex.split(os.getenv("COMFYUI_ARGS", "--listen 127.0.0.1 --port 8188"))
    models_dir = Path(os.getenv("MODELS_DIR", str(comfy_dir / "models")))
    if models_dir.resolve() == (comfy_dir / "models").resolve():
        return arguments
    config = {
        "comfy_cloud": {
            "base_path": str(models_dir),
            "is_default": True,
            **{key: key for key in MODEL_DIRECTORY_KEYS},
        }
    }
    config_path = Path(tempfile.gettempdir()) / "comfy-cloud-model-paths.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    return [*arguments, "--extra-model-paths-config", str(config_path)]


def main() -> None:
    comfy_dir = Path(os.getenv("COMFYUI_DIR", "/opt/ComfyUI"))
    _prepare_models()
    comfy = subprocess.Popen(
        [sys.executable, "main.py", *_comfy_arguments(comfy_dir)], cwd=comfy_dir
    )
    gateway = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "comfy_cloud.app:create_app",
            "--factory",
            "--host",
            "0.0.0.0",
            "--port",
            os.getenv("PORT", "8000"),
        ]
    )

    def stop(*_: object) -> None:
        for process in (gateway, comfy):
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while True:
            if comfy.poll() is not None or gateway.poll() is not None:
                stop()
                raise SystemExit(comfy.returncode or gateway.returncode or 1)
            time.sleep(0.5)
    finally:
        stop()


if __name__ == "__main__":
    main()
