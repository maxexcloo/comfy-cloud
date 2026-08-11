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

Compose starts `comfy-control control` and a local `comfy-control pod` worker.
Providers in `config/control.yaml` are enabled when their URL environment variable
is set. Add `CLIPROXY_MANAGEMENT_KEY` to display CLIProxyAPI usage.

Keep the controller's `/data` directory on persistent storage. It contains the
SQLite job history and controller-owned copies of generated media used by the
dashboard viewer. There is no automatic retention deletion.

## Providers

Definitions under `deploy/` run the same image on Modal, RunPod, SaladCloud and
Vast.ai. RunPod and Vast.ai contain separate Pod and Serverless definitions.

Use persistent storage for model weights and `JOBS_DIR` where the provider supports
it. The image defaults to `comfy-control pod`; override the container command with
`comfy-control serverless` when the ComfyUI frontend should not be exposed.
Vast.ai Serverless uses `comfy-control vast-serverless` for its request envelope.

The dashboard displays RunPod billing history, Vast.ai credit, Modal billing-cycle
spend, SaladCloud replica quota and CLIProxyAPI usage. SaladCloud monetary credit is
currently portal-only, so its public API contributes usage and quota instead.

## Model Preparation

`MODEL_PROFILES` defaults to `flux-2-klein-9b`. Set it to one or more
comma-separated profile names to change the prepared models. The supervisor prepares
those profiles before starting ComfyUI and the gateway. Hugging Face sources may use
`HF_TOKEN`; Civitai sources may use `CIVITAI_TOKEN`.

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
