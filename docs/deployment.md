# Deployment & Operations

## Standalone Stack

Copy `.env.example` to `.env`, replace every placeholder, then start the stack:

```bash
docker compose up --build --detach
docker compose ps
```

The stack uses version-pinned Bifrost and CLIProxyAPI images plus a locally built
Comfy Control image. `BIND_ADDRESS` defaults to `0.0.0.0`; use `127.0.0.1` when a
local reverse proxy is the only intended ingress.

Bifrost loads `config/bifrost.bootstrap.json` only into a fresh data volume. Later
provider and access changes live in Bifrost SQLite and must be managed through its
UI/API. Comfy Control reloads mounted `config/control.yaml` on restart.

Copy the provider variables from `.env.example` into `.env` before enabling the
matching example routes. Compose forwards only those declared provider values to
Comfy Control; it does not expose Bifrost or CLIProxy management secrets to the
controller.

Open CLIProxyAPI management and authenticate each desired CLI provider. Bifrost
routes its models through the `cliproxy` custom provider and media through
`comfy-control`.

## Container Images

GitHub Actions publishes:

- `ghcr.io/OWNER/ai-router:control` for the controller;
- generic worker branch, version and `latest` tags;
- `ghcr.io/OWNER/ai-router:flux-2-klein-4b` on `main`.

Pull requests build both services and smoke-test the worker. Git tags publish full,
minor and major version tags. The first GHCR package may need to be made public in
the repository owner’s package settings.

## Provider Deployments

Run `mise run setup`, fill `.mise.local.toml`, then inspect `mise tasks`. Provider
assets are grouped under `deploy/`:

| Provider   | Assets               | Idle Approach            |
| ---------- | -------------------- | ------------------------ |
| Modal      | `deploy/modal/`      | Automatic scale-down     |
| RunPod     | `deploy/runpod/`     | Stop pod or zero workers |
| SaladCloud | `deploy/saladcloud/` | Stop container group     |
| Vast.ai    | `deploy/vast/`       | Stop instance            |

Common first deployments use one 24-GB GPU, one worker and only
`flux-2-klein-4b`.

### Manager Actions

Each provider may declare HTTP `actions` such as `deploy`, `destroy` and `status`,
alongside its `lifecycle.start` and `lifecycle.stop` actions. They appear as buttons
in the authenticated Comfy Control UI. Use provider APIs directly where practical,
or a narrowly scoped provider-control webhook when a platform requires its own
CLI. Add a `confirmation` to destructive or expensive operations.

The scripts under each `deploy/<provider>/` directory remain bootstrap and
emergency entry points for creating the first worker or recovering when the
manager is unavailable. Mise tasks are intentionally thin wrappers around those
provider-owned implementations.

### Modal

Create the secret named by `MODAL_SECRET`, then run `mise run modal:deploy`.
`MODAL_MIN_CONTAINERS=0` and a 60-second scale-down window are the low-idle
defaults.

### RunPod

Run `mise run runpod:deploy`, or import `deploy/runpod/pod.json`. The persistent
volume downloads selected profiles once. For serverless, import
`deploy/runpod/serverless.json` as a load-balancer endpoint, attach a pre-populated
network volume and mount models under `/runpod-volume/models`.

### SaladCloud

Run `mise run salad:gpu-classes`, choose suitable GPU class IDs, set
`SALAD_IMAGE_NAME` and `MODEL_PROFILES`, then run `mise run salad:deploy`. Storage
is ephemeral, so use a weight-bearing profile image to avoid downloading weights
after every reallocation.

### Vast.ai

Run `mise run vast:offers`, set `VAST_OFFER_ID`, then run `mise run vast:deploy`.
Stopping preserves instance data; destroying does not. Vast Serverless requires
the PyWorker at `deploy/vast/worker.py` in the same image as the worker gateway.

## Open WebUI

Point Open WebUI’s common OpenAI-compatible connection at Bifrost `/v1`. Direct
Comfy Control integration examples remain under `examples/` for installations that
specifically need Open WebUI’s native ComfyUI or image-generation integration.

Anyone using a curated Open WebUI model must also have access to its selected base
model. After gateway changes, verify both a base-model completion and the curated
model rather than relying on `/v1/models` discovery alone. Set `BIFROST_URL`,
`BIFROST_API_KEY` and `GATEWAY_CHECK_MODEL` in `.mise.local.toml`, then run:

```bash
mise run gateway:check
```

## Important Limits

- Controller multipart uploads occupy its data volume until the job completes or
  fails.
- Destroy operations intentionally remove provider-local data; stop operations are
  the non-destructive idle path.
- The project does not grant model redistribution rights.
- Without object storage, outputs remain tied to the worker that produced them.
