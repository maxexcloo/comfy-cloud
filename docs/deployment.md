# Deployment & Operations

## Stack

Copy `.env.example` to `.env`, replace the required placeholders, then start the
stack:

```bash
docker compose up --build --detach
docker compose ps
```

Compose runs Bifrost, CLIProxyAPI and Comfy Control. Bifrost loads
`config/bifrost.bootstrap.json` only into a fresh data volume; later provider and
access changes belong in its UI/API. Comfy Control reads `config/control.yaml` on
every start.

Authenticate desired CLI providers through CLIProxyAPI management. Bifrost routes
language models through CLIProxyAPI and media models through Comfy Control.

## Worker

GitHub Actions publishes two images from `main`:

- `ghcr.io/OWNER/ai-router:control` for Comfy Control;
- `ghcr.io/OWNER/ai-router:worker` for the GPU worker and ComfyUI.

Pull requests build and smoke-test both images. Configure and deploy the worker in
your chosen GPU platform, then add its URL and API key to `config/control.yaml`.
The example uses the `WORKER_URL` and `WORKER_API_KEY` variables from `.env`.

Provider-specific worker definitions are retained under `deploy/` for Modal,
RunPod, SaladCloud and Vast.ai. They consume the same `worker` image; they are not
separate application stacks.

Provider management is deliberately API-only. Declare deploy, destroy, status,
start and stop requests in `config/control.yaml`; Comfy Control sends them from its
backend and exposes the actions in its authenticated UI. A deploy action may set
`resource_id_path` to capture the provider's returned identifier. Use
`{resource_id}` in later action URLs and the worker `base_url`; the identifier is
persisted in Comfy Control's SQLite volume.

[`config/control.example.yaml`](../config/control.example.yaml) is a complete
RunPod example using the official REST API. SaladCloud and Vast.ai use the same
declarative action mechanism. Modal is the small exception: its retained
`deploy/modal/app.py` definition uses Modal's programmatic Python deployment API,
and Modal handles scaling its web function to zero.

Provider API keys and the worker API key stay in `.env` and are expanded only in
the controller process. Provider responses are redacted before the UI displays
fields named like keys, passwords, secrets or tokens. Add confirmations to
destructive or expensive actions.

## Open WebUI

Point Open WebUI's common OpenAI-compatible connection at Bifrost `/v1`. Direct
Comfy Control integration examples remain under `examples/` for installations that
need Open WebUI's native ComfyUI or image-generation integration.

After gateway changes, verify both model discovery and a completion. Set
`BIFROST_API_KEY` in `.env`, then pass the model name to the check:

```bash
mise run gateway:check -- provider/model
```

## Important Limits

- Controller multipart uploads occupy its data volume until the job completes or
  fails.
- Destroy operations may remove provider data; stop is the non-destructive idle
  path.
- The project does not grant model redistribution rights.
- Without object storage, outputs remain tied to the worker that produced them.
