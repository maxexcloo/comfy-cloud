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

Deploy matching control and worker images. The controller uses the current
unversioned internal execution contract. A temporary legacy-worker fallback exists
only to support rolling upgrades and will be removed after managed providers have
been redeployed.

For a local GPU host:

```bash
cp .env.example .env
# Set CONTROL_API_KEY, CONTROL_SECRET_KEY and CONTROL_UI_PASSWORD.
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

Managed providers are credential-driven. Unconfigured providers remain visible and
link to Settings. When no named resource exists, the dashboard shows `Not deployed`
and offers Deploy. A routed request can also deploy the resource, wait for its
discovered URL to become healthy, forward the request, and apply the configured idle
policy. Provider resource identifiers are saved in the controller database; serving
URLs are rediscovered because they may change after a start or scale event.

Configure provider credentials, `HF_TOKEN`, model profiles, the worker image and GPU
preferences through Settings. Pin the worker image to a full release or SHA tag when
reproducibility is more important than following the moving `worker` tag. SaladCloud
also requires its organisation and project because its public API uses both slugs as
security boundaries. Comfy Control discovers a suitable current GPU class for
SaladCloud. RunPod and Vast.ai select suitable available GPU capacity through their
APIs. GPU classes, regions, worker counts and minimum Vast.ai GPU memory are optional
preferences rather than prerequisites.

On first start only, legacy optional values already present in `.env` are imported
into SQLite. This supports upgrades from environment-based releases. After confirming
the values in Settings, remove those optional entries from `.env`; subsequent starts
use the saved configuration. Back up `CONTROL_SECRET_KEY` with the data volume: a
replacement key cannot decrypt existing saved credentials.

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

Managed workers default to the `flux-2-klein-9b` profile. Change the selected profiles
in Settings. The supervisor prepares those profiles before starting ComfyUI and the
gateway. Hugging Face and Civitai credentials are also configured there.

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
