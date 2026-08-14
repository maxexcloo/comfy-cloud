import os
import subprocess
from collections.abc import Mapping

import modal


def serve() -> None:
    subprocess.Popen(["/opt/venv/bin/comfy-control", "serverless"])


def build_app(configuration: Mapping[str, str]) -> modal.App:
    app = modal.App("comfy-control")
    image = modal.Image.from_registry(
        configuration.get("WORKER_IMAGE", "ghcr.io/maxexcloo/comfy-control:worker"),
        force_build=True,
    ).entrypoint([])
    models = modal.Volume.from_name(
        configuration.get("MODAL_MODEL_VOLUME", "comfy-control-models"),
        create_if_missing=True,
    )
    environment = {"MODELS_DIR": "/models"}
    secret_values = {
        name: value
        for name in (
            "API_KEY",
            "CIVITAI_TOKEN",
            "CONTROL_UI_PASSWORD",
            "CONTROL_UI_USERNAME",
            "HF_TOKEN",
        )
        if (value := configuration.get(name))
    }
    secrets = [modal.Secret.from_dict(secret_values)] if secret_values else []
    for name in (
        "COMFYUI_REQUEST_TIMEOUT",
        "GENERATION_TIMEOUT",
        "GENERATION_QUEUE_LIMIT",
        "MAXIMUM_REQUEST_MIB",
        "MODEL_PROFILES",
    ):
        if (value := configuration.get(name)) or name == "MODEL_PROFILES":
            environment[name] = str(value or "")

    registered_serve = app.function(
        env=environment,
        gpu=configuration.get("MODAL_GPU", "L40S"),
        image=image,
        min_containers=int(configuration.get("MODAL_MIN_CONTAINERS", "0")),
        scaledown_window=int(configuration.get("MODAL_SCALEDOWN_WINDOW", "60")),
        secrets=secrets,
        volumes={"/models": models},
    )(modal.web_server(8000, startup_timeout=900)(serve))
    del registered_serve

    return app


app = build_app(os.environ)
