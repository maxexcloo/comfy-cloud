# Architecture

## Runtime

Each Comfy Control worker runs two supervised processes in one container:

- **Comfy Control worker** exposes authenticated internal execution and, in Pod
  mode, separately authenticated native ComfyUI routes on port 8000.
- **ComfyUI** executes workflows locally on port 8188 and is not modified by this
  project.

The `control` command is the public OpenAI-compatible service. It owns public model
IDs, ordered fallback, provider lifecycle, durable video jobs, account telemetry,
media provenance and the operations dashboard. Workers validate canonical
execution requests, render catalogue workflows and execute them in local ComfyUI.
Managed providers are selected by stable configured names. Their resource IDs and
serving URLs are discovered through provider APIs and kept in controller runtime
state, so provider-assigned addresses can change across starts without changing
deployment configuration.

## Configuration

The control plane has a deliberately small bootstrap environment:

- `CONTROL_API_KEY` authenticates inference clients;
- `CONTROL_SECRET_KEY` encrypts saved credentials;
- `CONTROL_UI_PASSWORD` and `CONTROL_UI_USERNAME` authenticate administrators.

Provider credentials, deployment preferences, routing, limits and worker settings
are versioned in SQLite. Secrets use authenticated encryption and are never returned
by the settings API. The administrator dashboard applies a validated configuration
snapshot atomically; updates are rejected while inference requests are active.

The packaged Python registry defines provider capabilities, public models and safe
control-plane requests. SQLite stores enabled routes and operator preferences,
while the UI exposes only known typed values. Arbitrary provider URLs, headers and
request bodies are not editable through the dashboard. `CONTROL_CONFIG` remains an
advanced explicit override for development and private provider catalogues.

## Request Flow

1. The control plane authenticates the request and selects configured targets.
   Public model IDs use ordered fallback. A qualified `provider/model` ID pins one
   provider and disables fallback for that request; provider aliases are accepted.
2. It starts a stopped provider when lifecycle controls are configured.
3. The worker checks the requested catalogue model against installed files and
   registered ComfyUI node types.
4. Portable request fields are copied into a fresh API-format workflow graph.
5. The controller submits a canonical request to the worker's unversioned
   `/internal/executions` endpoint. Workers do not expose the public API.
6. The request is admitted to the bounded queue.
7. GPU execution is serialised and submitted to local ComfyUI.
8. On a retriable failure, the control plane tries the next target and eventually
   CLI Proxy API with `grok-imagine-image-quality` for images and edits, or
   `grok-imagine-video-1.5` for video.
9. Results return as base64 or an authenticated URL.

A timed-out workflow is removed from the ComfyUI queue or interrupted when already
running.

## History and Media

The control plane records every image generation, image edit and video request in
SQLite. Explicit schema migrations preserve current data. History retains
sanitised parameters, every provider attempt, status, errors and timestamps. Input
and output media are content-addressed by SHA-256, include intrinsic dimensions or
duration where available, and are associated with their generation as ordered
inputs or outputs. The media library uses FTS-backed fuzzy prompt lookup, structured
parameter filters and source-to-derivative links. Assets remain viewable after a
worker is stopped or destroyed.
The dashboard uses a normal sign-in form backed by an HTTP-only signed session
cookie; it does not use browser Basic authentication. It queries each provider
control plane for current state without routing work to serverless capacity and
provides confirmed lifecycle controls, sanitised control logs, direct
provider-console links and a bounded image-generation test request. It opens images
and videos in a keyboard-accessible popup viewer after sign-in. History and events
are paginated in the dashboard. History, jobs, provider resources and generated
media remain in the persistent `data` volume; controller events are also persisted
there and bounded to the latest 2,000 records. Provider actions record their start,
success or failure, and display live controller logs in a closeable dialog.

Video jobs and input media are durable controller records. HTTP image inputs are
downloaded with private-network protection and bounded size; controller media URLs
and base64 data URLs can also be used as video inputs. Completed outputs are copied
from the worker before its compute can be released. Controller-owned history and
media remain available in the `data` volume until that volume is explicitly pruned.

## Runtime Modes

`comfy-control control` runs the central router and dashboard.
`comfy-control pod` serves the ComfyUI frontend with basic authentication.
`comfy-control serverless` blocks the frontend. Both commands provide:

- native ComfyUI proxy routes and WebSockets;
- `GET /health/live`, `/health/ready`, `/health` and `/metrics`;
- authenticated `/internal/info` and `/internal/executions` routes.

The gateway uses one API key. Terminate TLS and enforce client identity, quotas,
rate limits and deployment selection in an external trusted gateway. Health and
metrics endpoints intentionally remain unauthenticated and expose no generated
content.

`MAXIMUM_PENDING_GENERATIONS` defaults to `8`; `MAXIMUM_REQUEST_BYTES` defaults to
100 MiB.

`comfy-control vast-serverless` adds Vast.ai's required request-envelope ingress
in front of the normal Serverless worker.

Control and worker publish their live OpenAPI documents at `/openapi.json` and
interactive documentation at `/docs`. Controller operations are under `/ops`,
worker execution is under `/internal`, and both are unversioned current contracts.

## Provider Telemetry

Account telemetry is cached for one minute and cannot take inference offline.
CLI Proxy API reports request and token usage and obtains sanitised remaining Grok
allowances through its per-credential Management API proxy. RunPod reports Pod or
Endpoint billing history, Vast.ai reports account credit, Modal reports billing-cycle
spend and credits used, and SaladCloud reports public replica usage and quota.
