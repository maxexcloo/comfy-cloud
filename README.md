# Inference Control

A standalone personal AI gateway combining Bifrost, CLIProxyAPI, and Comfy Control.
It gives Open WebUI and other clients one authenticated OpenAI-compatible endpoint
for LLM, image, and video models, while GPU providers can start on demand and scale
back to zero.

```text
                         +-> CLIProxyAPI -> subscription-backed LLMs
Open WebUI -> Bifrost ---+
                         +-> Comfy Control -> provider worker -> stock ComfyUI
                                  |                    |
                               SQLite             S3 output
```

Bifrost owns the public gateway, authentication, LLM routing, and request logs.
CLIProxyAPI adapts authenticated CLI subscriptions. Comfy Control is the media
control plane and has two processes:

- **Control** is a small, always-on CPU container. It owns the public model names,
  ordered failover, provider start/stop hooks, durable video jobs, SQLite events,
  and the minimal status page.
- **Worker** packages stock ComfyUI with the workflow adapter. It runs beside the
  GPU, can scale to zero, and exposes OpenAI-compatible plus native ComfyUI APIs.

## Standalone stack

The root [`compose.yaml`](compose.yaml) starts all three services with persistent
volumes, health checks, Bifrost dashboard and inference authentication, and no
credentials committed to the repository.

```bash
cp .env.example .env
# Replace every replace-with-* value. `openssl rand -hex 32` is suitable.
docker compose up --build --detach
docker compose ps
```

The default endpoints are:

| Service                | URL                      |
| ---------------------- | ------------------------ |
| Bifrost gateway/UI     | `http://localhost:28080` |
| CLIProxyAPI management | `http://localhost:28317` |
| Comfy Control UI/API   | `http://localhost:28081` |

Use `BIFROST_ADMIN_USERNAME` and `BIFROST_ADMIN_PASSWORD` for the Bifrost UI.
Clients such as Open WebUI use `BIFROST_API_KEY` and the Bifrost base URL ending in
`/v1`. Bifrost seeds two custom OpenAI providers:

- `cliproxy/<model>` routes LLM calls to CLIProxyAPI.
- `comfy-control/<model>` routes image and video calls to Comfy Control.

Open CLIProxyAPI management with `CLIPROXY_MANAGEMENT_KEY` and authenticate the CLI
providers you want to expose. Its OAuth records persist in the `cliproxy` volume.
Add media routes and provider lifecycle actions to
[`standalone/control.yaml`](standalone/control.yaml), using
[`examples/control.yaml`](examples/control.yaml) as the reference, then restart
`comfy-control`.

Bifrost bootstraps its editable SQLite configuration from
[`standalone/bifrost.json`](standalone/bifrost.json) only when its database is
empty. Later changes should be made in the Bifrost UI. To deliberately bootstrap
the checked-in file again, stop the stack and remove only the `bifrost` volume.

The Compose ports bind to all interfaces by default. Set `BIND_ADDRESS=127.0.0.1`
when the stack should only be reachable through a local reverse proxy or tunnel.

## Control quick start

Copy [`examples/control.yaml`](examples/control.yaml), remove any unused providers,
and use environment variables for secrets. Targets are tried in order. A provider
without lifecycle actions is assumed to be serverless and is woken by the request
itself; configured actions are called before first use and after the idle timeout.

```yaml
models:
  - id: flux/text-to-image
    operation: image_generation
    targets:
      - { provider: primary, model: flux-2-klein-4b/text-to-image }
      - { provider: fallback, model: flux-2-klein-4b/text-to-image }

providers:
  - id: primary
    api_key: env.PRIMARY_COMFY_API_KEY
    base_url: env.PRIMARY_COMFY_URL
    idle_seconds: 600
```

```yaml
services:
  comfy-control:
    image: ghcr.io/maxexcloo/inference-control:control
    ports: ["28081:8000"]
    environment:
      CONTROL_API_KEY: change-me
      PRIMARY_COMFY_API_KEY: change-me
      PRIMARY_COMFY_URL: https://worker.example.com
    volumes:
      - ./control.yaml:/config/control.yaml:ro
      - comfy-control:/data

volumes:
  comfy-control:
```

The dashboard is at `/` and uses HTTP Basic with `CONTROL_UI_USERNAME` (`comfy` by
default). Its password is `CONTROL_UI_PASSWORD`, falling back to the controller API
key when unset. The dashboard is intentionally read-only and shows provider state,
recent jobs, and lifecycle/request events.

## Worker capabilities

- `GET /health/live`, `/health/ready`, and `/health` report readiness; `GET /metrics` exposes standard Prometheus metrics. Responses carry an `x-request-id`.
- `GET /v1/models` lists registered workflows as models.
- Hugging Face model sources are pinned in `profiles/`; selected profiles can be prepared automatically and large files can be split into GHCR-safe packs with the CLI.
- `MODE=pod` serves the ComfyUI frontend at `/`.
- `MODE=serverless` exposes the APIs but returns 404 for the frontend.
- Native ComfyUI routes (`/prompt`, `/history`, `/view`, `/queue`, `/object_info`, `/ws`, and uploads) are proxied unchanged.
- `POST /v1/images/generations`, `/v1/images/edits`, and `/v1/videos` select a workflow with the `model` field.

ComfyUI remains unmodified. The gateway is a sidecar process in the same container.
Catalogue entries with `required_files` are only advertised when every file exists
under `MODELS_DIR` (default `/opt/ComfyUI/models`).

## How worker requests flow

1. The supervisor prepares selected model profiles, then starts stock ComfyUI on
   its private port and the gateway on the public port.
2. The gateway loads catalogue manifests and their checksum-pinned API-format
   workflows.
3. Model discovery checks both required files and ComfyUI's registered node types.
4. An OpenAI request selects a catalogue model; portable values are copied into
   the configured workflow node inputs.
5. The gateway admits a bounded number of requests, serialises GPU execution,
   submits the graph to ComfyUI, and polls its history.
6. Outputs are returned as base64, an authenticated local URL, or an object-storage
   URL. For scale-to-zero video work, the controller holds the worker request open,
   records the job in SQLite, and returns the public asynchronous job immediately.

Native ComfyUI requests bypass workflow translation but use the same authentication
and upstream ComfyUI process.

## Worker development

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
secrets and provider identifiers. No model profile is selected by default. Add
`MODEL_PROFILES` with one or more comma-separated bundled profile names when a
deployment should prepare models automatically.

The provider CLIs and API dependencies are managed by Mise. Run
`mise tasks` for the complete control surface. Each provider has explicit
`deploy`, `status`, `start`/`stop`, and `destroy` tasks where its API supports
them; destructive tasks are deliberately separate. Mise does not build or publish
images: GitHub Actions handles both images automatically after each push to `main`.

- `modal:*`: deploy, inspect logs/status, and stop the app.
- `runpod:*`: deploy, list, inspect, start, stop, and destroy pods.
- `salad:*`: deploy, inspect GPUs/logs/status, start, stop, and destroy a container group.
- `vast:*`: find offers, deploy, inspect logs/status, start, stop, and destroy instances.

SaladCloud has no first-party CLI comparable to `runpodctl` or `vastai`, so its
tasks call the supported REST API directly with `curl` and `jq`.

Prebuilt images are also published by GitHub Actions:

```bash
docker pull ghcr.io/maxexcloo/inference-control:latest
```

Pushes to `main` publish the generic `latest` and `main` tags plus the
weight-bearing `flux-2-klein-4b` tag. Git tags such as `v0.1.0` also publish
`v0.1.0`, `0.1.0`, and `0.1`. Commit-SHA image tags are deliberately not
published. The first GHCR package may need its visibility changed to public in
the repository owner's package settings.

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

### Ordered image-edit references

`POST /v1/images/edits` accepts ordered reference images as repeated multipart
fields named `image`. The selected workflow determines the exact required count.
Legacy image-edit models continue to require one `image`; the bundled FLUX.2 Klein
9B multi-reference models require exactly two, three, or four:

- `flux-2-klein-9b/image-edit-2-reference`
- `flux-2-klein-9b/image-edit-3-reference`
- `flux-2-klein-9b/image-edit-4-reference`

Reference purpose is expressed only through positional prompt language. For example:

> Create a full-body photograph of both women together. Preserve the identity and
> body shape of the woman from image 1 as Mara. Preserve the identity and body shape
> of the woman from image 2 as Elise.

```bash
curl http://localhost:8000/v1/images/edits \
  -H 'Authorization: Bearer local-secret' \
  -F 'model=flux-2-klein-9b/image-edit-2-reference' \
  -F 'prompt=Create a photograph of the person from image 1 with the person from image 2.' \
  -F 'image=@mara.png' \
  -F 'image=@elise.png'
```

Model discovery reports the exact count as
`capabilities.reference_images.minimum` and `.maximum`. Inference Control does not
store identities, rewrite prompts, assign character roles, build collages, or run
face matching; callers own those semantics and must refer to images by position.

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
comfy-control workflow-add \
  --id flux-2-klein-4b/text-to-image \
  --operation image_generation \
  --workflow flux-klein-api.json \
  --mapping flux-klein-mapping.yaml \
  --catalogue-dir /data/catalogue
```

Restart the container after changing the catalogue. This intentionally avoids a
mutable administration service.

Mount the custom directory and set `CATALOGUE_DIR=/data/catalogue`; writing the
files alone does not register the directory with the running gateway. New entries
created by `workflow-add` include a workflow checksum automatically. Run
`comfy-control repository-check` for bundled catalogue/profile consistency.

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

The `minimax-h3-ref2va` profile is intentionally download-only: no portable stock
reference-to-video catalogue workflow is bundled yet.

## Durable jobs and object storage

Video jobs are persisted as atomically replaced JSON in `JOBS_DIR` (default:
disabled). Point it at a POSIX-compatible shared mounted volume to keep job records
across restarts and make them visible to sibling workers. Active jobs carry a
lease; an abandoned job becomes failed after its lease expires. Legacy records
without a lease are failed on restart.

Set S3-compatible credentials to upload generated images and videos instead of
proxying them through the gateway, and to return signed URLs that survive the
worker that produced them:

```bash
S3_ENDPOINT_URL=https://...   # S3, R2, MinIO
S3_BUCKET=comfy-control
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

## Runtime limits and security

The gateway deliberately uses one shared API key. Terminate TLS and apply
internet-facing identity, per-client quotas, and request-rate controls at the
provider load balancer or a dedicated reverse proxy.

`MAXIMUM_PENDING_GENERATIONS` defaults to `8` and bounds the admitted GPU queue;
excess requests receive HTTP 429. `MAXIMUM_REQUEST_BYTES` defaults to 100 MiB and
rejects oversized uploads and proxied request bodies. A timed-out gateway workflow
is removed from the ComfyUI queue or interrupted when it is the running prompt.
Health, readiness, RunPod ping, and Prometheus endpoints remain unauthenticated so
provider probes and monitoring systems can reach them; they expose no generated
content.

## Bifrost and Open WebUI

Point Bifrost at the **controller**, not at an individual worker. The current
Bifrost OpenAI provider supports image generation, image edits, and video; use
[`examples/bifrost-provider.json`](examples/bifrost-provider.json) as a custom
OpenAI provider so it has its own name and base URL. Keep LLM fallbacks in Bifrost
and media-worker failover in Comfy Control.

Open WebUI can either use Bifrost as its common OpenAI endpoint or point its image
generation connection directly at Comfy Control. Direct worker integrations are
still available when you specifically want the native ComfyUI workflow UI:

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
`MODELS_DIR/.comfy-control`; unchanged files are reused, downloads are installed
atomically, and a filesystem lock prevents multiple workers from downloading the
same profile concurrently. Readiness stays false until preparation finishes.

Profiles can still be fetched manually:

```bash
comfy-control models-fetch profiles/flux-2-klein-4b.yaml \
  --models-dir /opt/ComfyUI/models
```

Use the matching profile for the other bundled workflows:

```bash
comfy-control models-fetch profiles/flux-2-klein-9b.yaml --models-dir /opt/ComfyUI/models
comfy-control models-fetch profiles/krea-2-turbo.yaml --models-dir /opt/ComfyUI/models
comfy-control models-fetch profiles/minimax-h3-fl2va.yaml --models-dir /opt/ComfyUI/models
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

For ephemeral SaladCloud nodes, GitHub automatically publishes
`ghcr.io/OWNER/inference-control:flux-2-klein-4b` after the generic image succeeds on
`main`. The jobs share the runtime layer cache, so only the model layer differs.
Larger profiles remain on persistent volumes: the 9B and MiniMax sets exceed
SaladCloud's image limit, and keeping them out of the runtime image avoids slow
pulls for every provider. Publishing weights may impose upstream licence
obligations; repository owners remain responsible for reviewing them before
enabling package access.

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
comfy-control pack model.safetensors model-pack/
comfy-control unpack model-pack/model.safetensors.pack.json /opt/ComfyUI/models/diffusion_models
```

The chunk directory can be copied into separate OCI layers or published as an OCI
artefact. Reconstruction verifies every part and the final file.

## Deployment

For a first deployment:

1. Push `main` and wait for the `container` workflow to publish
   `ghcr.io/OWNER/inference-control:latest`.
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
- SaladCloud: run `mise run salad:gpu-classes`, select suitable UUIDs, then explicitly set `SALAD_IMAGE_NAME` and `MODEL_PROFILES` before `mise run salad:deploy`. GitHub publishes the named `flux-2-klein-4b` image automatically, but Salad does not select it by default. Salad storage is ephemeral, so the generic image would download selected weights after every reallocation.
- Vast.ai: run `mise run vast:offers`, set `VAST_OFFER_ID`, then run `mise run vast:deploy`. Stopping preserves instance data; destroying does not.

For the first test, use one 24 GB GPU, one replica/worker, and only
`flux-2-klein-4b`. Adding every profile increases download time, storage, VRAM
requirements, and the chance of paying for idle capacity without making a single
request faster.

Workers behind Comfy Control still need an HTTP/load-balancing serverless product
rather than a queue endpoint that wraps requests in a provider-specific `/run`
envelope. Vast Serverless requires a PyWorker ingress: run
`deploy/vast_worker.py` inside the same container as the gateway and point Vast's
`PYWORKER_REPO` at this repository. The published image includes `aiohttp` and the
worker. It preserves methods, query strings, multipart uploads, and streamed
responses while injecting the gateway bearer key.

## Important limits

- Multipart image-to-video uploads are staged under the controller data volume and removed when the job completes or fails. Size the volume for the largest accepted upload plus the SQLite job history.
- Without S3 storage, completed outputs live only on the worker that produced them and downloading one can wake that provider again. Configure S3 for clean scale-to-zero video delivery.
- URL image responses proxy ComfyUI output and require the same bearer key when storage is not configured; `b64_json` is the default and most portable response.
- The project does not grant model redistribution rights. Review and comply with every upstream licence before publishing weight-bearing images or packs.

## Licence

AGPL-3.0 - see [LICENSE](LICENSE).
