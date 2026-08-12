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

Compose starts only `comfy-control control`. An externally managed local Pod is
enabled when `LOCAL_POD_URL` is set. Managed providers are enabled by their Modal,
RunPod, SaladCloud or Vast.ai management credential. The controller discovers
resources by their configured stable name and refreshes provider-assigned worker
URLs at runtime; do not store those URLs in deployment configuration. Add
`CLIPROXY_MANAGEMENT_KEY` to display CLI Proxy API usage.

Keep the controller's `/data` directory on persistent storage. It contains the
SQLite job history and controller-owned copies of generated media used by the
dashboard viewer. There is no automatic retention deletion.

## Providers

Definitions under `deploy/` run the worker image on Modal, RunPod, SaladCloud and
Vast.ai. RunPod and Vast.ai contain separate Pod and Serverless definitions.

Managed providers are credential-driven. When no named resource exists, the dashboard
shows `Not deployed` and offers Deploy. A routed request can also deploy the resource,
wait for its discovered URL to become healthy, forward the request, and apply the
configured idle policy. Provider resource identifiers are saved in the controller
database; serving URLs are rediscovered because they may change after a start or scale
event.

Compose forwards every setting in `.env.example` to the control plane. Settings such
as `HF_TOKEN`, `MODEL_PROFILES` and `WORKER_IMAGE` are then applied when it creates a
managed worker. Set `WORKER_IMAGE` to a full release or SHA tag to pin provider
deployments instead of following the moving `worker` tag.

SaladCloud also requires `SALAD_ORGANISATION` and `SALAD_PROJECT` because its public API
uses both slugs as security boundaries. Comfy Control discovers a suitable current GPU
class for SaladCloud. RunPod and Vast.ai select suitable available GPU capacity through
their APIs. `RUNPOD_DATA_CENTRES`, `RUNPOD_GPU_TYPES`, `RUNPOD_MAXIMUM_WORKERS`,
`SALAD_GPU_CLASSES`, `VAST_MAXIMUM_WORKERS`, `VAST_MINIMUM_GPU_RAM_GB` and
`VAST_MINIMUM_GPU_RAM_MB` are optional overrides rather than prerequisites.

Deploy creates all provider-side compute dependencies: a Pod or container group for
Pod-style providers, a RunPod template and endpoint for RunPod Serverless, and a Vast.ai
template, endpoint and workergroup for Vast.ai Serverless. Stop preserves a reusable Pod
or container group. Terminate deletes its compute resource; provider-local disks may be
deleted with that resource. Generated media is copied to the controller's persistent
`/data` volume before idle shutdown and does not depend on provider storage.

Use persistent storage for model weights and `JOBS_DIR` where the provider supports
it. The worker image defaults to `comfy-control pod`; override the container
command with `comfy-control serverless` when the ComfyUI frontend should not be
exposed. Vast.ai Serverless uses `comfy-control vast-serverless` for its request
envelope.

The dashboard displays RunPod billing history, Vast.ai credit, Modal billing-cycle
spend, SaladCloud replica quota and CLI Proxy API usage. SaladCloud monetary credit is
currently portal-only, so its public API contributes usage and quota instead.
Comfy Control derives CLI Proxy API request totals from its durable history and reads
Grok allowances from CLI Proxy API's authenticated account data. CLI Proxy API v7
removed the legacy aggregate usage route, while its replacement is a destructive
collector queue and must not be polled by a dashboard. Keep
`usage-statistics-enabled` set to `true` for CLI Proxy API's own telemetry.

## Model Preparation

`MODEL_PROFILES` defaults to `flux-2-klein-9b`. Set it to one or more
comma-separated profile names to change the prepared models. The supervisor prepares
those profiles before starting ComfyUI and the gateway. Hugging Face sources may use
`HF_TOKEN`; Civitai sources may use `CIVITAI_TOKEN`.

Profiles can also be baked into an image by passing `MODEL_PROFILE` as a build
argument with the corresponding build secret.

## Output Storage

The controller copies successful images and videos into `/data/media` before a
worker is stopped. Keep `/data` on persistent storage; API image URLs and completed
video content are then served by Comfy Control with normal authentication.

## Important Limits

- The project does not grant model redistribution rights.
- Publishing model weights may impose upstream licence obligations.
- Worker-local jobs and outputs disappear when their persistent storage is
  destroyed.
