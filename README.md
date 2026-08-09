# comfy-cloud

A small, stock-first ComfyUI container for Modal, RunPod, SaladCloud, and Vast.ai.
It exposes both the native ComfyUI API and an OpenAI-compatible workflow catalogue,
so the same deployment can be used by Bifrost, Open WebUI, the ComfyUI browser, or
direct clients.

## What works

- `GET /health/live`, `/health/ready`, and `/health` report readiness; `GET /metrics` exposes Prometheus counters. Responses carry an `x-request-id`.
- `GET /v1/models` lists registered workflows as models.
- Hugging Face model sources are pinned in `profiles/`; selected profiles can be prepared automatically and large files can be split into GHCR-safe packs with the CLI.
- `MODE=pod` serves the ComfyUI frontend at `/`.
- `MODE=serverless` exposes the APIs but returns 404 for the frontend.
- Native ComfyUI routes (`/prompt`, `/history`, `/view`, `/queue`, `/object_info`, `/ws`, and uploads) are proxied unchanged.
- `POST /v1/images/generations`, `/v1/images/edits`, and `/v1/videos` select a workflow with the `model` field.

ComfyUI remains unmodified. The gateway is a sidecar process in the same container.
Catalogue entries with `required_files` are only advertised when every file exists
under `MODELS_DIR` (default `/opt/ComfyUI/models`).

## Quick start

Development is driven by mise, matching the homelab repos:

```bash
mise run check             # Prek hooks + pytest
mise run modal:deploy      # deploy the Modal app
mise run runpod:deploy     # create a RunPod pod
mise run salad:deploy      # create a SaladCloud container group
mise run setup             # create local configuration and install Prek hooks
mise run vast:deploy       # create a Vast.ai instance
```

Copy `.mise.local.toml.default` to `.mise.local.toml` (gitignored) and fill in
secrets, provider identifiers, and the profile you want to run. `MODEL_PROFILES`
accepts comma-separated bundled profile names and defaults to
`flux-2-klein-4b` in the template.

The provider CLIs, GitHub CLI, and JSON tooling are managed by Mise. Run
`mise tasks` for the complete control surface. Each provider has explicit
`deploy`, `status`, `start`/`stop`, and `destroy` tasks where its API supports
them; destructive tasks are deliberately separate.

- `image:*`: request and inspect profile-image builds on GitHub.
- `modal:*`: deploy, inspect logs/status, and stop the app.
- `runpod:*`: deploy, list, inspect, start, stop, and destroy pods.
- `salad:*`: deploy, inspect GPUs/logs/status, start, stop, and destroy a container group.
- `vast:*`: find offers, deploy, inspect logs/status, start, stop, and destroy instances.

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
  -e COMFY_UI_PASSWORD=local-ui-secret \
  -e COMFY_UI_USERNAME=comfy \
  -e MODE=pod \
  -e MODEL_PROFILES=flux-2-klein-4b \
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

Catalogue entries live beside API-format ComfyUI workflow JSON. The manifest maps
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
  --catalogue-dir /data/catalogue
```

Restart the container after changing the catalogue. This intentionally avoids a
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

Eight stock, self-hosted native workflows are bundled and become discoverable
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

The published image includes object-storage support. For a source installation,
install the `s3` extra with `pip install '.[s3]'`. Persisted jobs store the object
key and mint a fresh presigned URL for each request. When storage is unavailable
or an upload fails, the gateway falls back to proxying ComfyUI output.

## Bifrost and Open WebUI

Use `examples/bifrost-provider.json` with the deployment base URL ending in `/v1`.
Bifrost discovers registered workflows through `/v1/models`.

Open WebUI can use either integration:

- `examples/openwebui-comfyui.env`: Open WebUI supplies its own API workflow and calls the native routes.
- `examples/openwebui-openai.env`: workflow selection through the `model` field.

## Model packs

Keep the generic runtime and workflows in the image; keep weights in a mounted
volume or separate profile image. The normal deployment path prepares profiles
before ComfyUI starts:

```bash
MODEL_PROFILES=flux-2-klein-4b
```

Preparation is safe to repeat. A profile digest and file sizes are recorded under
`MODELS_DIR/.comfy-cloud`; unchanged files are reused, downloads are installed
atomically, and a filesystem lock prevents multiple workers from downloading the
same profile concurrently. Readiness stays false until preparation finishes.

Profiles can still be fetched manually:

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
| `krea-2-turbo`                 | ~15 GB  | 48 GB | ~$1.05      |
| `minimax-h3-fl2va`             | ~60 GB  | 80 GB | ~$4.20      |

For a first test, fetch the 4B distilled profile alone — the fastest model and
the cheapest volume.

For ephemeral SaladCloud nodes, request the small profile image instead:

```bash
mise run image:profile
```

This triggers GitHub to publish
`ghcr.io/OWNER/comfy-cloud:flux-2-klein-4b`. It is manual because publishing
weights may impose upstream licence obligations. Larger profiles remain on
persistent volumes: the 9B and MiniMax sets exceed SaladCloud's image limit, and
keeping them out of the runtime image avoids slow pulls for every provider.

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
artefact. Reconstruction verifies every part and the final file.

## Deployment

For a first deployment:

1. Push `main` and wait for the `container` workflow to publish
   `ghcr.io/OWNER/comfy-cloud:latest`.
2. Make the first GHCR package public, or configure provider registry credentials.
3. Run `mise run setup`, set real secrets and provider identifiers in
   `.mise.local.toml`, then choose one `provider:deploy` task below.
4. Wait for automatic profile preparation, then confirm `GET /health/ready`
   returns 200.

The gateway refuses to start with absent or placeholder API credentials. A fresh
deployment advertises a workflow only after its required files are available.

| Provider          | Model storage         | Idle approach            | Best fit                                |
| ----------------- | --------------------- | ------------------------ | --------------------------------------- |
| Modal             | Modal Volume          | 60-second scale-down     | Clean managed API and bursty traffic    |
| RunPod pod        | Pod/network volume    | Explicit `runpod:stop`   | Simplest and fastest first deployment   |
| RunPod serverless | Network volume        | Zero workers + FlashBoot | Irregular direct API traffic            |
| SaladCloud        | Profile image         | Explicit `salad:stop`    | Cheapest interruptible 4B inference     |
| Vast.ai           | Instance/local volume | Explicit `vast:stop`     | Cheap experiments with manual lifecycle |

- Modal: create the secret referenced by `MODAL_SECRET`, then run `mise run modal:deploy`. `MODAL_MIN_CONTAINERS=0` and a 60-second scale-down window are the low-idle defaults.
- RunPod pod: run `mise run runpod:deploy`, or import `deploy/runpod-template.json`. The default persistent volume downloads the selected profile once.
- RunPod serverless: import `deploy/runpod-serverless.json` as a **Load Balancer** endpoint, attach a pre-populated network volume, enable FlashBoot, use zero active workers, and mount models under `/runpod-volume/models`.
- SaladCloud: run `mise run salad:gpu-classes`, select suitable UUIDs, build the `flux-2-klein-4b` profile image, then run `mise run salad:deploy`. Salad storage is ephemeral, so the generic image would download weights after every reallocation.
- Vast.ai: run `mise run vast:offers`, set `VAST_OFFER_ID`, then run `mise run vast:deploy`. Stopping preserves instance data; destroying does not.

For the first test, use one 24 GB GPU, one replica/worker, and only
`flux-2-klein-4b`. Adding every profile increases download time, storage, VRAM
requirements, and the chance of paying for idle capacity without making a single
request faster.

For direct Bifrost/Open WebUI compatibility, use an HTTP/load-balancing serverless
product rather than a queue endpoint that wraps requests in a provider-specific
`/run` envelope. Vast Serverless requires a PyWorker ingress: run
`deploy/vast_worker.py` inside the same container as the gateway and point Vast's
`PYWORKER_REPO` at this repository. The published image includes `aiohttp` and the
worker. It preserves methods, query strings, multipart uploads, and streamed
responses while injecting the gateway bearer key.

## Important limits

- Without `JOBS_DIR`, video job state is held in the worker process. Without S3 storage, completed outputs live only on the worker that produced them. Configure both for scale-to-zero.
- URL image responses proxy ComfyUI output and require the same bearer key when storage is not configured; `b64_json` is the default and most portable response.
- The project does not grant model redistribution rights. Review and comply with every upstream licence before publishing weight-bearing images or packs.

## Licence

AGPL-3.0 - see [LICENSE](LICENSE).
