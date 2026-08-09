# Workflow source

This API-format graph follows ComfyUI's official two-reference **FLUX.2 Klein 9B
Distilled Image Edit** template at commit
[`f160442`](https://github.com/Comfy-Org/workflow_templates/blob/f1604424815ffde8fed20543ac38bf245807fbca/templates/image_flux2_klein_image_edit_9b_distilled.json).

The template's subgraph was flattened into ComfyUI API format. Each reference is
scaled, VAE-encoded, then chained through `ReferenceLatent` for both positive and
zeroed conditioning, preserving the official input order and first-reference output
dimensions.
