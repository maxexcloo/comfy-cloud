# comfy-cloud

A small, stock-first ComfyUI container for Modal, RunPod, and Vast.ai. It exposes
both the native ComfyUI API and an OpenAI-compatible workflow catalog, so the same
deployment can be used by Bifrost, Open WebUI, the ComfyUI browser, or direct clients.

## What works

- `GET /v1/models` lists registered workflows as models.
- `POST /v1/images/generations`, `/v1/images/edits`, and `/v1/videos` select a workflow with the `model` field.
- Native ComfyUI routes (`/prompt`, `/history`, `/view`, `/queue`, `/object_info`, `/ws`, and uploads) are proxied unchanged.
- `MODE=pod` serves the ComfyUI frontend at `/`.
- `MODE=serverless` exposes the APIs but returns 404 for the frontend.
- Hugging Face model sources are pinned in `profiles/`; large files can be split into GHCR-safe packs with the CLI.

ComfyUI remains unmodified. The gateway is a sidecar process in the same container.
Catalog entries with `required_files` are only advertised when every file exists
under `MODELS_DIR` (default `/opt/ComfyUI/models`).

## Quick start

Prebuilt images are published by GitHub Actions:

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
  prompt: {node: "6", input: text}
  width: {node: "12", input: width}
  height: {node: "12", input: height}
  seed: {node: "25", input: noise_seed}
output: {node: "31", type: image}
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

Three stock, self-hosted native workflows are bundled and become discoverable
after their required files are installed:

- `flux-2-klein-4b/text-to-image`
- `flux-2-klein-4b/image-edit`
- `flux-2-klein-9b/text-to-image`
- `flux-2-klein-9b/image-edit`
- `krea-2-turbo/text-to-image`
- `minimax-h3/text-to-video`

Image edits accept a multipart form with `image`, `prompt`, and optional `n`,
`seed`, `steps`, and `response_format` (`b64_json` or `url`). Dimensions follow
the uploaded image, so `size` is not supported for edits. The Flux 2 Klein edit
workflows use the native `ReferenceLatent` reference-conditioning nodes, so no
custom nodes are required.

MiniMax accepts OpenAI-style `size` and `seconds`; seconds are snapped to H3's
native frame grid. Generation returns a video job that can be polled through
`GET /v1/videos/{id}` and downloaded from `GET /v1/videos/{id}/content`.

## Bifrost and Open WebUI

Use `examples/bifrost-provider.json` with the deployment base URL ending in `/v1`.
Bifrost discovers registered workflows through `/v1/models`.

Open WebUI can use either integration:

- `examples/openwebui-openai.env`: workflow selection through the `model` field.
- `examples/openwebui-comfyui.env`: Open WebUI supplies its own API workflow and calls the native routes.

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
`/run` envelope. Vast Serverless requires its PyWorker ingress; configure handlers
for the routes you use and point its model server at this gateway on port `8000`.

## Important limits

- Video job state is held in the worker process. Keep a worker alive for the job, or add durable job/object storage before using scale-to-zero video in production.
- URL image responses proxy ComfyUI output and require the same bearer key; `b64_json` is the default and most portable response.
- The project does not grant model redistribution rights. Review and comply with every upstream license before publishing weight-bearing images or packs.
