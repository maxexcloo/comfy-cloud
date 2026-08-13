# Catalogue & Models

## Workflows

Each directory under `catalogue/` contains a manifest beside its checksum-pinned
API-format ComfyUI workflow JSON. The manifest maps portable API fields to concrete
node inputs:

Directory names use `<profile>-<operation>`, where the operation is `t2i`, `edit`,
`i2v` or `t2v`. Each manifest's `profile` must exactly match a
`profiles/<profile>.yaml` file, and its ID uses `<profile>/<operation-name>`.

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

## Profiles

Profiles under `profiles/` pin weight sources independently from workflow logic.
The control-plane Settings page defaults to `flux-2-klein-9b`; select one or more
profile names there to change the models prepared on managed workers. Fetch a
profile manually with:

```bash
comfy-control models-fetch profiles/flux-2-klein-9b.yaml \
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
