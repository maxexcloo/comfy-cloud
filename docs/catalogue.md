# Catalogue & Models

## Workflows

Each directory under `catalogue/` contains a manifest beside its checksum-pinned
API-format ComfyUI workflow JSON. The manifest maps portable API fields to concrete
node inputs:

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

Set `CATALOGUE_DIR=/data/catalogue`, mount that directory and restart the worker.
UI-format ComfyUI workflows are not accepted; export them using **Export (API)**.
Run `comfy-control repository-check` after catalogue or profile changes.

## Bundled Models

The catalogue includes text-to-image, image-edit and MiniMax H3 video workflows.
Multi-reference FLUX workflows require exactly two, three or four ordered `image`
parts. Reference meaning belongs in the prompt; Comfy Control does not assign character
roles or rewrite prompts.

Model discovery reports the supported reference count as
`capabilities.reference_images.minimum` and `.maximum`.

Image edits accept `image`, `prompt`, and optional `n`, `seed`, `steps` and
`response_format`. Their dimensions follow the uploaded image. MiniMax video
requests accept `prompt`, `size` and `seconds`; image-to-video also accepts the
first-frame `image`.

## Profiles

Profiles under `profiles/` pin weight sources independently from workflow logic.
Select one or more comma-separated profile names with `MODEL_PROFILES`. Fetch a
profile manually with:

```bash
comfy-control models-fetch profiles/flux-2-klein-4b.yaml \
  --models-dir /opt/ComfyUI/models
```

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

## Object Storage

Configure S3-compatible output storage when generated files must outlive a worker:

```bash
S3_ENDPOINT_URL=https://...
S3_BUCKET=comfy-control
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_REGION=auto
S3_PREFIX=outputs
S3_URL_EXPIRES=3600
```

`S3_PUBLIC_BASE_URL` may replace presigned URLs. Without object storage, completed
outputs remain on the producing worker and downloading them may wake that provider.
