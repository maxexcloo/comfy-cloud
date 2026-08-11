# Architecture

## Runtime

Comfy Control runs two supervised processes in one container:

- **Comfy Control gateway** exposes authenticated OpenAI-compatible and native
  ComfyUI routes on port 8000.
- **ComfyUI** executes workflows locally on port 8188 and is not modified by this
  project.

The `control` command fronts independently deployed workers and owns public model
IDs, ordered fallback, provider lifecycle, durable video jobs, account telemetry
and the operations dashboard. Workers remain self-contained ComfyUI gateways.
Managed providers are selected by stable configured names. Their resource IDs and
serving URLs are discovered through provider APIs and kept in controller runtime
state, so provider-assigned addresses can change across starts without changing
deployment configuration.

## Request Flow

1. The control plane authenticates the request and selects configured targets.
   Public model IDs use ordered fallback. A qualified `provider/model` ID pins one
   provider and disables fallback for that request; provider aliases are accepted.
2. It starts a stopped provider when lifecycle controls are configured.
3. The worker checks the requested catalogue model against installed files and registered
   ComfyUI node types.
4. Portable request fields are copied into a fresh API-format workflow graph.
5. The request is admitted to the bounded queue.
6. GPU execution is serialised and submitted to local ComfyUI.
7. On a retriable failure, the control plane tries the next target and eventually
   CLIProxyAPI with `grok-imagine-image-quality` for images and edits, or
   `grok-imagine-video-1.5` for video.
8. Results return as base64, an authenticated local URL or a durable
   object-storage URL.

A timed-out workflow is removed from the ComfyUI queue or interrupted when already
running.

## History and Media

The control plane records every image generation, image edit and video request in
SQLite. History retains sanitised parameters, provider, status, errors and
timestamps. Successful output media is copied into the controller's persistent
`media/` directory and remains viewable after a worker is stopped or destroyed.
The dashboard uses a normal sign-in form backed by an HTTP-only signed session
cookie; it does not use browser Basic authentication. It queries each provider
control plane for current state without routing work to serverless capacity and
provides confirmed lifecycle controls, sanitised control logs, direct
provider-console links and a bounded image-generation test request. It opens images
and videos in a popup viewer after sign-in.

Video requests also create durable worker-local records. When `JOBS_DIR` is
configured, worker job transitions survive process restarts. Completed outputs can
be streamed from ComfyUI or uploaded to S3-compatible object storage.

Provider-scale durability requires object storage because worker-local output is
lost when the worker and its volume are destroyed. Controller-owned history remains
available in its `control-data` volume. Media is retained until that volume is
explicitly pruned; `CONTROL_MAXIMUM_MEDIA_BYTES` limits each archived file and
defaults to 2 GiB.

## Runtime Modes

`comfy-control control` runs the central router and dashboard.
`comfy-control pod` serves the ComfyUI frontend with basic authentication.
`comfy-control serverless` blocks the frontend. Both commands provide:

- native ComfyUI proxy routes and WebSockets;
- `GET /health/live`, `/health/ready`, `/health` and `/metrics`;
- `GET /v1/models`;
- OpenAI-compatible image generation, image editing and video endpoints.

The gateway uses one API key. Terminate TLS and enforce client identity, quotas,
rate limits and deployment selection in an external trusted gateway. Health and
metrics endpoints intentionally remain unauthenticated and expose no generated
content.

`MAXIMUM_PENDING_GENERATIONS` defaults to `8`; `MAXIMUM_REQUEST_BYTES` defaults to
100 MiB.

`comfy-control vast-serverless` adds Vast.ai's required request-envelope ingress
in front of the normal Serverless worker.

## Provider Telemetry

Account telemetry is cached for one minute and cannot take inference offline.
CLIProxyAPI reports request and token usage and obtains sanitised remaining Grok
allowances through its per-credential Management API proxy. RunPod reports Pod or
Endpoint billing history, Vast.ai reports account credit, Modal reports billing-cycle
spend and credits used, and SaladCloud reports public replica usage and quota.
