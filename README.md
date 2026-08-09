# comfy-cloud

A small, stock-first ComfyUI container for Modal, RunPod, and Vast.ai. It exposes
both the native ComfyUI API and an OpenAI-compatible workflow catalog, so the same
deployment can be used by Bifrost, Open WebUI, the ComfyUI browser, or direct clients.

## What works

- `GET /health/live`, `/health/ready`, and `/health` report readiness; `GET /metrics` exposes Prometheus counters. Responses carry an `x-request-id`.
- `GET /v1/models` lists registered workflows as models.
- Hugging Face model sources are pinned in `profiles/`; large files can be split into GHCR-safe packs with the CLI.
- `MODE=pod` serves the ComfyUI frontend at `/`.
- `MODE=serverless` exposes the APIs but returns 404 for the frontend.
- Native ComfyUI routes (`/prompt`, `/history`, `/view`, `/queue`, `/object_info`, `/ws`, and uploads) are proxied unchanged.
- `POST /v1/images/generations`, `/v1/images/edits`, and `/v1/videos` select a workflow with the `model` field.

ComfyUI remains unmodified. The gateway is a sidecar process in the same container.
Catalog entries with `required_files` are only advertised when every file exists
under `MODELS_DIR` (default `/opt/ComfyUI/models`).

## Quick start

Development is driven by mise, matching the homelab repos:

```bash
mise run check           # Prek hooks + pytest
mise run deploy-modal    # deploy the Modal app
mise run deploy-runpod   # create a RunPod pod
mise run deploy-vast     # create a Vast.ai instance
mise run fmt             # Prettier + Ruff formatting
mise run setup           # create local configuration and install Prek hooks
```

Copy `.mise.local.toml.default` to `.mise.local.toml` (gitignored) and fill in
secrets — API keys, `HF_TOKEN`/`CIVITAI_TOKEN`, S3 credentials, and the provider
values (`MODAL_TOKEN_ID/SECRET`, `RUNPOD_API_KEY`, `VAST_API_KEY`,
`VAST_OFFER_ID`). Non-secret defaults live in the `[env]` section of
`.mise.toml`.

The provider CLIs are managed by mise: `modal` and `vastai` via pipx, and
`runpodctl` via ubi. `mise install` fetches all tools from `.mise.toml`.

- `deploy-modal`: `modal deploy deploy/modal_app.py`
- `deploy-runpod`: `runpodctl create pod ...` from the template values
- `deploy-vast`: `vastai create instance ...` (needs `VAST_OFFER_ID` from `vastai search offers`)

Prebuilt images are also published by GitHub Actions:

```bash
docker pull ghcr.io/maxexcloo/comfy-cloud:latest
```

Pushes to `main` publish `latest`, `main`, and `sha-...` tags. Git tags such as
`v0.1.0` also publish `v0.1.0`, `0.1.0`, and `0.1`. The first GHCR package may
need its visibility changed to public in the repository owner's package settings.

```bash
docker build -t comfy-cloud .
docker run --gpus all --rm -p 8000:8000 \
  -e API_KEY=local-secret \
  -e COMFY_UI_USERNAME=comfy \
  -e COMFY_UI_PASSWORD=local-ui-secret \
  -e MODE=pod \
  -v "$PWD/models:/opt/ComfyUI/models" \
  comfy-cloud
```

Then use:

- ComfyUI: `http://localhost:8000/`
- Native API: `http://localhost:8000/prompt`
- OpenAI API: `http://localhost:8000/v1`

Bearer authentication is accepted everywhere. In pod mode, browser requests can
also use HTTP Basic authentication.

There is deliberately no separate `MODE=api`: the API is available in both modes.
Use `pod` when you want the browser UI and `serverless` when you do not.

```bash
curl http://localhost:8000/v1/models \
  -H 'Authorization: Bearer local-secret'
```

```bash
curl http://localhost:8000/v1/images/generations \
  -H 'Authorization: Bearer local-secret' \
  -H 'Content-Type: application/json' \
  -d '{"model":"example/checkpoint-text-to-image","prompt":"studio portrait","size":"1024x1024"}'
```

## Workflows are models

Catalog entries live beside API-format ComfyUI workflow JSON. The manifest maps
portable API fields to concrete node inputs:

```yaml
id: flux-2-klein-4b/text-to-image
profile: flux-2-klein-4b
operation: image_generation
workflow: workflow.json
input_map:
  prompt: { node: "6", input: text }
  width: { node: "12", input: width }
  height: { node: "12", input: height }
  seed: { node: "25", input: noise_seed }
output: { node: "31", type: image }
```

Register an exported API workflow without modifying the image:

```bash
comfy-cloud workflow-add \
  --id flux-2-klein-4b/text-to-image \
  --operation image_generation \
  --workflow flux-klein-api.json \
  --mapping flux-klein-mapping.yaml \
  --catalog-dir /data/catalog
```

Restart the container after changing the catalog. This intentionally avoids a
mutable administration service.

The bundled `example/checkpoint-text-to-image` workflow demonstrates the format;
replace its checkpoint or register the official API workflow for the installed
profile. UI-format workflow templates cannot be submitted directly—export them
from ComfyUI using **Export (API)** first.

A workflow is only advertised through `/v1/models` and `/health` when both its
`required_files` exist and every `class_type` in its workflow graph is registered
by the running ComfyUI (`/object_info`). A workflow whose nodes are missing is
hidden and rejected if addressed directly, so a stale workflow never appears
runnable against an older ComfyUI image.

Three stock, self-hosted native workflows are bundled and become discoverable
after their required files are installed:

- `flux-2-klein-4b/image-edit`
- `flux-2-klein-4b/text-to-image`
- `flux-2-klein-9b/image-edit`
- `flux-2-klein-9b/text-to-image`
- `flux-2-klein-base-9b/text-to-image`
- `krea-2-turbo/text-to-image`
- `minimax-h3/image-to-video`
- `minimax-h3/text-to-video`

Image edits accept a multipart form with `image`, `prompt`, and optional `n`,
`seed`, `steps`, and `response_format` (`b64_json` or `url`). Dimensions follow
the uploaded image, so `size` is not supported for edits. The Flux 2 Klein edit
workflows use the native `ReferenceLatent` reference-conditioning nodes, so no
custom nodes are required.

`flux-2-klein-base-9b/text-to-image` uses the slower, higher-quality undistilled
base checkpoint (20 steps, cfg 5.0) versus the 4-step distilled `flux-2-klein-9b`.
Both share the same text encoder and decoder, so fetching the `flux-2-klein-9b`
profile installs the weights for both workflows.

MiniMax H3 text-to-video accepts a JSON body with `prompt`, `size`, and
`seconds`. Image-to-video (`minimax-h3/image-to-video`) instead accepts a
multipart form with an `image` first frame plus `prompt`, `size`, and
`seconds`, and drives the native `first_frame` conditioning.

MiniMax accepts OpenAI-style `size` and `seconds`; seconds are snapped to H3's
native frame grid. Generation returns a video job that can be polled through
`GET /v1/videos/{id}` and downloaded from `GET /v1/videos/{id}/content`.

## Durable jobs and object storage

Video jobs are persisted as JSON in `JOBS_DIR` (default: disabled). Point it at a
mounted volume to keep job records across worker restarts; any job that was
queued or in progress when the worker stopped is reported as failed with a
"worker restarted" error.

Set S3-compatible credentials to upload generated images and videos instead of
proxying them through the gateway, and to return signed URLs that survive the
worker that produced them:

```bash
S3_ENDPOINT_URL=https://...   # S3, R2, MinIO
S3_BUCKET=comfy-cloud
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=auto                # default us-east-1
S3_PREFIX=outputs             # default outputs
S3_PUBLIC_BASE_URL=           # optional: use public URLs instead of presigned
S3_URL_EXPIRES=3600           # presigned URL lifetime in seconds
```

Object storage requires `boto3` (install the `s3` extra: `pip install '.[s3]'`).
When storage is unavailable or an upload fails, the gateway falls back to
proxying ComfyUI output, so storage is strictly additive.

## Bifrost and Open WebUI

Use `examples/bifrost-provider.json` with the deployment base URL ending in `/v1`.
Bifrost discovers registered workflows through `/v1/models`.

Open WebUI can use either integration:

- `examples/openwebui-comfyui.env`: Open WebUI supplies its own API workflow and calls the native routes.
- `examples/openwebui-openai.env`: workflow selection through the `model` field.

## Model packs

Keep the generic runtime and workflows in the image; keep weights in a mounted
volume or separate model image. Fetch a pinned bundled profile into that volume:

```bash
comfy-cloud models-fetch profiles/flux-2-klein-4b.yaml \
  --models-dir /opt/ComfyUI/models
```

Use the matching profile for the other bundled workflows:

```bash
comfy-cloud models-fetch profiles/flux-2-klein-9b.yaml --models-dir /opt/ComfyUI/models
comfy-cloud models-fetch profiles/krea-2-turbo.yaml --models-dir /opt/ComfyUI/models
comfy-cloud models-fetch profiles/minimax-h3-fl2va.yaml --models-dir /opt/ComfyUI/models
```

Fetch only what you run — each profile is independent, and persistent model
storage is billed per GB. Approximate sizes and monthly volume cost at
~$0.07/GB:

| Profile                        | Weights | VRAM  | ~Storage/mo |
| ------------------------------ | ------- | ----- | ----------- |
| `flux-2-klein-4b`              | ~8 GB   | 16 GB | ~$0.56      |
| `flux-2-klein-9b` (incl. base) | ~40 GB  | 24 GB | ~$2.80      |
| `krea-2-turbo`                 | ~15 GB  | 24 GB | ~$1.05      |
| `minimax-h3-fl2va`             | ~60 GB  | 80 GB | ~$4.20      |

For a first test, fetch the 4B distilled profile alone — the fastest model and
the cheapest volume.

Hugging Face sources require a pinned `revision` and optionally use `HF_TOKEN`.
Civitai sources use an immutable `version_id`, optional `filename`, a ComfyUI-relative
`destination`, and optional `sha256`; private downloads use `CIVITAI_TOKEN`:

```yaml
sources:
  - type: civitai
    version_id: 123456
    filename: model.safetensors
    destination: checkpoints/model.safetensors
    sha256: RECOMMENDED_FULL_SHA256
```

GHCR limits an individual layer to 10 GB. Split large files into 8 GiB verified chunks:

```bash
comfy-cloud pack model.safetensors model-pack/
comfy-cloud unpack model-pack/model.safetensors.pack.json /opt/ComfyUI/models/diffusion_models
```

The chunk directory can be copied into separate OCI layers or published as an OCI
artifact. Reconstruction verifies every part and the final file. The provided
profile files describe upstream sources but do not download gated weights during
container startup.

## Deployment

- Modal: set `COMFY_CLOUD_IMAGE`, create the secret referenced by `MODAL_SECRET`, then run `modal deploy deploy/modal_app.py`.
- RunPod pod: import `deploy/runpod-template.json`, replace the image and secrets, and attach model storage.
- RunPod serverless: import the image as a **Load Balancer** endpoint, expose port `8000`, set `PORT=8000`, `PORT_HEALTH=8000`, and `MODE=serverless`. The image implements RunPod's `/ping` readiness contract.
- Vast.ai: use `deploy/vast-template.json` as the starting template and attach adequate disk/model storage.

For direct Bifrost/Open WebUI compatibility, use an HTTP/load-balancing serverless
product rather than a queue endpoint that wraps requests in a provider-specific
`/run` envelope. Vast Serverless requires a PyWorker ingress: run
`deploy/vast_worker.py` inside the same container as the gateway (with `aiohttp`
installed) and point Vast's `PYWORKER_REPO` at a repository containing it. The
worker forwards the OpenAI-compatible and native ComfyUI routes to the gateway,
injects the bearer key, and streams responses back unchanged.

## Important limits

- Without `JOBS_DIR`, video job state is held in the worker process. Without S3 storage, completed outputs live only on the worker that produced them. Configure both for scale-to-zero.
- URL image responses proxy ComfyUI output and require the same bearer key when storage is not configured; `b64_json` is the default and most portable response.
- The project does not grant model redistribution rights. Review and comply with every upstream license before publishing weight-bearing images or packs.
