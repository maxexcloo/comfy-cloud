# Catalogue & Models

## Workflows

Each directory under `catalogue/` contains a manifest beside its checksum-pinned
API-format ComfyUI workflow JSON. The manifest maps portable API fields to concrete
node inputs:

Directory names use `<profile>-<operation>`, where the operation identifies the
workflow implementation. Each manifest's `profile` must exactly match a
`catalogue/profiles/<profile>.yaml` file, and its ID uses
`<profile>/<operation-name>`.
Source-free profiles with zero minimum VRAM describe pipelines made entirely from
built-in ComfyUI nodes.

```yaml
id: flux-2-klein-9b/text-to-image
profile: flux-2-klein-9b
operation: image_generation
workflow: workflow.json
input_map:
  prompt: { node: "4", input: text }
  width: { node: "6", input: width }
  height: { node: "6", input: height }
  seed: { node: "7", input: noise_seed }
output: { node: "13", type: image }
```

A workflow is advertised only when all `required_files` exist and every workflow
`class_type` is registered by ComfyUI. Stale or incomplete workflows are hidden and
rejected when addressed directly.

Register a custom exported API workflow without modifying the image:

```bash
comfy-control workflow-add \
  --id custom/text-to-image \
  --operation image_generation \
  --workflow workflow-api.json \
  --mapping mapping.yaml \
  --catalogue-dir /data/catalogue
```

Set `CATALOGUE_DIR=/data/catalogue` on a manually managed worker, mount that
directory and restart the worker.
UI-format ComfyUI workflows are not accepted; export them using **Export (API)**.
Run `comfy-control repository-check` after catalogue or profile changes.

## Bundled Models

The catalogue includes text-to-image, single-image edit and MiniMax H3 video
workflows. Reference meaning belongs in the prompt; Comfy Control does not assign
character roles or rewrite prompts.

Image edits accept `image`, `prompt`, and optional `n`, `seed`, `steps` and
`response_format`. Their dimensions follow the uploaded image. MiniMax video
requests accept `prompt`, `size` and `seconds`; image-to-video also accepts the
first-frame `image`.

## Image upscaling

`POST /v1/images/upscales` routes an uploaded image through the same configured
image-provider order as generation. The bundled
`image-upscale/realesrgan-x4plus` pipeline uses ComfyUI's native model-upscale
nodes and the pinned RealESRGAN x4plus model. It accepts a final `scale` from
greater than 1 through 4 and defaults to 2×:

```bash
curl -D response-headers.txt \
  -H "Authorization: Bearer ${CONTROL_API_KEY}" \
  -F image=@source.png \
  -F model=image-upscale \
  -F response_format=url \
  -F scale=2 \
  https://comfy-control.example/v1/images/upscales
```

The response includes `x-comfy-duration-seconds`, `x-comfy-history-id` and
`x-comfy-provider`. History stores the model and requested scale; Media records the
source and output dimensions and links both assets. Every request first performs
the native 4× AI enhancement. A 2× or 3× request then downsamples that enhanced
result to its requested final dimensions. This makes the runs directly comparable
by provider, elapsed time, file size and visual result.

## Profiles

Profiles under `catalogue/profiles/` pin weight sources independently from
workflow logic while keeping all model catalogue data in one place.
The control-plane Settings page defaults to `flux-2-klein-9b`; select one or more
profile names there to change the models prepared on managed workers. Fetch a
profile manually with:

```bash
comfy-control models-fetch catalogue/profiles/flux-2-klein-9b.yaml \
  --models-dir /opt/ComfyUI/models
```

Automation can read or replace the desired set through the typed current API at
`GET` or `PUT /ops/model-packages`; its schema is published in `/openapi.json`.
Changing the desired set takes effect when managed providers are next deployed.

Hugging Face sources require a pinned `revision` and may use `HF_TOKEN`. Civitai
sources use an immutable `version_id`, a ComfyUI-relative `destination`, and may
include `filename` and `sha256`.

Fetch only the profiles a deployment runs. Weight storage and GPU requirements are
independent costs; bundling more profiles does not make a request faster.

GHCR limits individual layers to 10 GB. Large files can be split into verified
8-GiB chunks:

```bash
comfy-control pack model.safetensors model-pack/
comfy-control unpack model-pack/model.safetensors.pack.json \
  /opt/ComfyUI/models/diffusion_models
```

Publishing model weights may impose upstream licence obligations. Review each
model licence before enabling public package access.
