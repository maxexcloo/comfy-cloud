# Deployment & Operations

## Container

GitHub Actions publishes separate control-plane and worker images from `main`:

```text
ghcr.io/OWNER/comfy-control:control
ghcr.io/OWNER/comfy-control:worker
```

Pushing a semantic release tag such as `v0.1.0` also publishes immutable release
tags and shortened minor-version tags:

```text
ghcr.io/OWNER/comfy-control:0.1.0-control
ghcr.io/OWNER/comfy-control:0.1.0-worker
ghcr.io/OWNER/comfy-control:0.1-control
ghcr.io/OWNER/comfy-control:0.1-worker
```

Deployment assets follow the moving role tags by default. Pin a full release tag
when reproducibility is more important than automatic updates. Every build also
receives a role-specific `sha-<short-sha>-control` or
`sha-<short-sha>-worker` tag.

The lightweight control image contains the API, dashboard and provider control
plane. The CUDA-enabled worker image contains Comfy Control, pinned ComfyUI, the
bundled catalogue and model profiles. Run workers on CUDA-capable hosts with
persistent model and output storage.

For a local GPU host:

```bash
cp .env.example .env
docker compose up --build --detach
docker compose ps
```

Pull requests build both images and smoke-test their packaged commands.

Compose starts `comfy-control control` and a local `comfy-control pod` worker.
The local Pod is enabled when `LOCAL_POD_URL` is set. Managed providers are enabled
by their Modal, RunPod, SaladCloud or Vast.ai management credential. The controller
discovers resources by their configured stable name and refreshes provider-assigned
worker URLs at runtime; do not store those URLs in deployment configuration. Add
`CLIPROXY_MANAGEMENT_KEY` to display CLIProxyAPI usage.

Keep the controller's `/data` directory on persistent storage. It contains the
SQLite job history and controller-owned copies of generated media used by the
dashboard viewer. There is no automatic retention deletion.

## Providers

Definitions under `deploy/` run the worker image on Modal, RunPod, SaladCloud and
Vast.ai. RunPod and Vast.ai contain separate Pod and Serverless definitions.

Existing managed resources must use the names in `config/control.yaml`. SaladCloud
also requires `SALAD_ORGANISATION` and `SALAD_PROJECT` because its API paths are
scoped by both names. Provider API credentials and the shared `WORKER_API_KEY` are
still injected through the secret store; discovered resource IDs and serving URLs
remain controller runtime state.

Use persistent storage for model weights and `JOBS_DIR` where the provider supports
it. The worker image defaults to `comfy-control pod`; override the container
command with `comfy-control serverless` when the ComfyUI frontend should not be
exposed. Vast.ai Serverless uses `comfy-control vast-serverless` for its request
envelope.

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
