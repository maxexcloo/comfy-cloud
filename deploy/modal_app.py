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


@app.function(
    image=image,
    gpu=os.getenv("MODAL_GPU", "L40S"),
    volumes={"/opt/ComfyUI/models": models},
    secrets=[modal.Secret.from_name(os.getenv("MODAL_SECRET", "comfy-cloud"))],
    scaledown_window=300,
)
@modal.web_server(8000, startup_timeout=900)
def serve():
    subprocess.Popen(["python", "-m", "comfy_cloud.supervisor"])
