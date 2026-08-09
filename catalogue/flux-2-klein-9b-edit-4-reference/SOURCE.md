# Workflow source

This API-format graph extends ComfyUI's official two-reference **FLUX.2 Klein 9B
Distilled Image Edit** template at commit
[`f160442`](https://github.com/Comfy-Org/workflow_templates/blob/f1604424815ffde8fed20543ac38bf245807fbca/templates/image_flux2_klein_image_edit_9b_distilled.json).

The template's subgraph was flattened into ComfyUI API format. The third and fourth
references repeat its documented scale, VAE-encode, and ordered `ReferenceLatent`
chain for both positive and zeroed conditioning; first-reference output dimensions
are kept.
