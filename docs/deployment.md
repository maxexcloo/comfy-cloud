# Deployment & Operations

## Container

GitHub Actions publishes one image from `main`:

```text
ghcr.io/OWNER/comfy-control:main
```

The image contains Comfy Control, pinned ComfyUI, the bundled catalogue and model
profiles. Run it on a CUDA-capable host with persistent model and output storage.

For a local GPU host:

```bash
cp .env.example .env
docker compose up --build --detach
docker compose ps
```

Pull requests build the image and smoke-test its packaged command.

## Providers

Definitions under `deploy/` show how to run the same image on Modal, RunPod,
SaladCloud and Vast.ai. These assets configure a Comfy Control instance; provider
selection and lifecycle orchestration remain external concerns.

Use persistent storage for model weights and `JOBS_DIR` where the provider supports
it. The image defaults to `comfy-control pod`; override the container command with
`comfy-control serverless` when the ComfyUI frontend should not be exposed.

## Model Preparation

Set `MODEL_PROFILES` to one or more comma-separated profile names. The supervisor
prepares those profiles before starting ComfyUI and the gateway. Hugging Face
sources may use `HF_TOKEN`; Civitai sources may use `CIVITAI_TOKEN`.

Profiles can also be baked into an image by passing `MODEL_PROFILE` as a build
argument with the corresponding build secret.

## Object Storage

Configure S3-compatible storage when generated output must outlive its worker.
Without object storage, URLs and completed output remain tied to the worker and its
volume.

## Important Limits

- The project does not grant model redistribution rights.
- Publishing model weights may impose upstream licence obligations.
- Worker-local jobs and outputs disappear when their persistent storage is
  destroyed.
