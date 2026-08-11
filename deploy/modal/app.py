import os
import subprocess

import modal

app = modal.App("comfy-control")
image = modal.Image.from_registry(
    os.getenv("WORKER_IMAGE", "ghcr.io/maxexcloo/comfy-control:worker"),
    add_python="3.11",
).entrypoint([])
models = modal.Volume.from_name(
    os.getenv("MODAL_MODEL_VOLUME", "comfy-control-models"), create_if_missing=True
)
environment = {
    "JOBS_DIR": "/opt/ComfyUI/models/.comfy-control/jobs",
    "MODELS_DIR": "/opt/ComfyUI/models",
}
secret_environment = (
    {
        "API_KEY": os.getenv("WORKER_API_KEY"),
        "CIVITAI_TOKEN": os.getenv("CIVITAI_TOKEN"),
        "COMFY_UI_PASSWORD": os.getenv("COMFY_UI_PASSWORD"),
        "COMFY_UI_USERNAME": os.getenv("COMFY_UI_USERNAME"),
        "HF_TOKEN": os.getenv("HF_TOKEN"),
    }
    if modal.is_local()
    else {}
)
secrets = modal.Secret.from_dict(secret_environment)
for name in (
    "MAXIMUM_PENDING_GENERATIONS",
    "MAXIMUM_REQUEST_BYTES",
    "MODEL_PROFILES",
    "PUBLIC_BASE_URL",
):
    if value := os.getenv(name):
        environment[name] = value


@app.function(
    gpu=os.getenv("MODAL_GPU", "L40S"),
    image=image,
    min_containers=int(os.getenv("MODAL_MIN_CONTAINERS", "0")),
    scaledown_window=int(os.getenv("MODAL_SCALEDOWN_WINDOW", "60")),
    env=environment,
    secrets=[secrets],
    volumes={"/opt/ComfyUI/models": models},
)
@modal.web_server(8000, startup_timeout=900)
def serve():
    subprocess.Popen(["comfy-control", "serverless"])
