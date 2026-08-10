# Architecture

## Components

AI Router separates general inference routing from media-worker execution:

- **Bifrost** is the public authenticated gateway. It owns language-model routing,
  provider selection, access control and request logs.
- **CLIProxyAPI** adapts authenticated CLI subscriptions into an OpenAI-compatible
  language-model endpoint.
- **Comfy Control** is an always-on CPU service. It owns public media model names,
  ordered provider failover, provider management actions, durable jobs and its
  status UI.
- **Comfy Control worker** runs beside stock ComfyUI. It translates portable
  OpenAI image/video requests into checksum-pinned ComfyUI workflows.

ComfyUI remains unmodified. The worker gateway is a sidecar process in the same
container.

## Media Request Flow

1. Bifrost forwards the selected public media model to Comfy Control.
2. Comfy Control chooses the first healthy configured provider and runs its start
   lifecycle action when required.
3. The worker prepares selected model profiles before becoming ready.
4. The worker verifies required files and registered ComfyUI node types.
5. Portable request values are copied into the selected API-format workflow.
6. GPU execution is serialised and bounded by the pending-request limit.
7. Results return as base64, an authenticated worker URL or durable object-storage
   URL. Video jobs remain queryable through Comfy Control.

Provider deployment and lifecycle controls are explicit HTTP actions in
`config/control.yaml`. The authenticated UI invokes only declared actions and
requires a same-origin confirmation header, so it does not provide a general
command-execution surface.

Native ComfyUI requests bypass workflow translation but share authentication and
the upstream ComfyUI process.

## State Ownership

| State                        | Owner                           |
| ---------------------------- | ------------------------------- |
| Bifrost live configuration   | Bifrost SQLite volume           |
| Bifrost first-start defaults | `config/bifrost.bootstrap.json` |
| Catalogue and workflows      | `catalogue/`                    |
| Controller routes/providers  | `config/control.yaml`           |
| Model source pins            | `profiles/`                     |
| OAuth records                | CLIProxyAPI volume              |
| Video job history            | Comfy Control SQLite/data       |
| Worker model weights         | Provider volume or image        |

Changing the Bifrost bootstrap file does not update an existing Bifrost database.
Use the Bifrost UI/API for live changes, or deliberately recreate only the Bifrost
volume after backing up any state that must be retained.

## Runtime Boundaries

`MODE=pod` serves the ComfyUI frontend. `MODE=serverless` exposes APIs without the
frontend. Both modes provide:

- native ComfyUI proxy routes;
- `GET /health/live`, `/health/ready`, `/health` and `/metrics`;
- `GET /v1/models`;
- OpenAI-compatible image generation, image editing and video endpoints.

The worker uses one shared API key. Terminate TLS and enforce client identity,
quotas and rate limits at Bifrost or another trusted reverse proxy. Health and
metrics endpoints intentionally remain unauthenticated and expose no generated
content.

`MAXIMUM_PENDING_GENERATIONS` defaults to `8`; `MAXIMUM_REQUEST_BYTES` defaults to
100 MiB. A timed-out workflow is removed from the ComfyUI queue or interrupted
when already running.
