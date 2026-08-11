# Architecture

## Runtime

Comfy Control runs two supervised processes in one container:

- **Comfy Control gateway** exposes authenticated OpenAI-compatible and native
  ComfyUI routes on port 8000.
- **ComfyUI** executes workflows locally on port 8188 and is not modified by this
  project.

Each instance controls one local ComfyUI runtime. It can also use CLIProxyAPI as a
fixed fallback when ComfyUI is unavailable or execution fails. An external gateway
may route between independently deployed Comfy Control instances.

## Request Flow

1. The gateway authenticates the request and enforces its size limit.
2. The requested catalogue model is checked against installed files and registered
   ComfyUI node types.
3. Portable request fields are copied into a fresh API-format workflow graph.
4. The request is admitted to the bounded queue.
5. GPU execution is serialised and submitted to local ComfyUI.
6. If ComfyUI is unavailable or execution fails, the request is retried through
   CLIProxyAPI with `grok-imagine-image-quality` for images and edits, or
   `grok-imagine-video-1.5` for video.
7. Results return as base64, an authenticated local URL or a durable
   object-storage URL.

A timed-out workflow is removed from the ComfyUI queue or interrupted when already
running.

## Video Jobs

Video requests create durable worker-local records. When `JOBS_DIR` is configured,
job transitions survive process restarts. Completed outputs can be streamed from
ComfyUI or uploaded to S3-compatible object storage.

Provider-scale durability requires object storage because worker-local output is
lost when the worker and its volume are destroyed.

## Runtime Modes

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
