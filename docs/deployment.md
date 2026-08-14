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

Every build receives role-specific `sha-<short-sha>-control` and
`sha-<short-sha>-worker` tags. A published control image automatically deploys the
worker image built from the same source revision. An explicit `WORKER_IMAGE`
override can instead select another immutable release or digest.

The lightweight control image contains the API, dashboard and provider control
plane. The CUDA-enabled worker image contains Comfy Control, pinned ComfyUI, the
bundled catalogue and model profiles. Run workers on CUDA-capable hosts with
persistent model and output storage. Its CUDA 13.0 base matches the highest
minimum CUDA version currently accepted by managed RunPod endpoints.

Deploy matching control and worker images. The controller uses the current
unversioned internal execution contract; compatibility with older worker APIs is
not retained.

For a local GPU host:

```bash
cp .env.example .env
# Set CONTROL_API_KEY, CONTROL_SECRET_KEY and CONTROL_UI_PASSWORD.
docker compose up --build --detach
docker compose ps
```

Pull requests build both images and smoke-test their packaged commands.

Compose starts only `comfy-control control`. Managed providers are enabled by their Modal,
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

Configure provider credentials, `HF_TOKEN`, model profiles and GPU preferences
through Settings. The packaged controller locks its worker image to the matching
source revision. Set `WORKER_IMAGE` only to override that default with another
immutable release tag or digest. SaladCloud also requires its organisation and
project because its public API uses both slugs as security boundaries. Comfy Control
discovers a suitable current GPU class for SaladCloud. RunPod and Vast.ai select
suitable available GPU capacity through their APIs. GPU classes, regions, worker
counts and minimum Vast.ai GPU memory are optional preferences rather than
prerequisites.

Optional provider and worker environment variables override their corresponding
SQLite settings on every start. The dashboard marks those fields as controlled by
the environment and does not allow them to be edited. Removing a variable restores
the previously stored value. Back up `CONTROL_SECRET_KEY` with the data volume: a
replacement key cannot decrypt existing saved credentials.

Deploy creates all provider-side compute dependencies: a Pod or container group for
Pod-style providers, a RunPod template and endpoint for RunPod Serverless, and a Vast.ai
template, endpoint and workergroup for Vast.ai Serverless. Stop preserves a reusable Pod
or container group. Terminate deletes its compute resource; provider-local disks may be
deleted with that resource. Generated media is copied to the controller's persistent
`/data` volume before idle shutdown and does not depend on provider storage.

Use persistent storage for model weights where the provider supports it. Video job
state and generated media are owned by the controller. The worker image defaults to
`comfy-control pod`; override the container
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

Managed workers default to the `flux-2-klein-9b` package. Install or remove pinned
packages in Settings, then redeploy a provider. Before starting ComfyUI, the
supervisor prepares the selected package's models and LoRAs and removes files
tracked only by deselected packages. It never removes unmanaged files. Hugging Face
and Civitai credentials are also configured there.

Profiles can also be baked into an image by passing `MODEL_PROFILE` as a build
argument with the corresponding build secret.

## Output Storage

The controller copies successful images and videos into `/data/media` before a
worker is stopped. Keep `/data` on persistent storage; API image URLs and completed
video content are then served by Comfy Control with normal authentication.

## Importing a Grok catalogue export

`catalogue-import` imports the complete `assets.jsonl`, `media.jsonl`,
`messages.jsonl`, and `images/` export layout into the current control database:

```sh
comfy-control catalogue-import /path/to/catalog-research \
  --database /data/comfy-control.db
```

The command verifies every local asset against its SHA-256 digest, copies it into
managed media storage, preserves source records and links as indexed parameters,
and retains metadata-only media and conversation messages in history. Original
message timestamps are preserved. Exports without media timestamps use the export
files' modification time.

Import identifiers are deterministic, so interrupted or repeated imports are
safe: existing histories and media links are skipped.

## Important Limits

- The project does not grant model redistribution rights.
- Publishing model weights may impose upstream licence obligations.
- Worker-local ComfyUI outputs are transient; the controller copies successful
  results into its persistent media store.
