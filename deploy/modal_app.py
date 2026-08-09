import os
import subprocess

import modal

app = modal.App("comfy-cloud")
image = modal.Image.from_registry(
    os.environ["COMFY_CLOUD_IMAGE"],
    add_python="3.11",
).entrypoint([])
models = modal.Volume.from_name(
    os.getenv("MODAL_MODEL_VOLUME", "comfy-cloud-models"), create_if_missing=True
)
environment = {"MODELS_DIR": "/opt/ComfyUI/models"}
if model_profiles := os.getenv("MODEL_PROFILES"):
    environment["MODEL_PROFILES"] = model_profiles


@app.function(
    gpu=os.getenv("MODAL_GPU", "L40S"),
    image=image,
    min_containers=int(os.getenv("MODAL_MIN_CONTAINERS", "0")),
    scaledown_window=int(os.getenv("MODAL_SCALEDOWN_WINDOW", "60")),
    env=environment,
    secrets=[modal.Secret.from_name(os.getenv("MODAL_SECRET", "comfy-cloud"))],
    volumes={"/opt/ComfyUI/models": models},
)
@modal.web_server(8000, startup_timeout=900)
def serve():
    subprocess.Popen(["python", "-m", "comfy_cloud.supervisor"])
