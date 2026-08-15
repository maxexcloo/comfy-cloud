# Comfy Control

Comfy Control is an authenticated OpenAI-compatible control plane for ComfyUI. It
routes image and video requests across Pod and Serverless deployments, controls
provider lifecycles, and reports usage and credit. Public model IDs use configured
provider fallback only for the same exact model. CLI Proxy API models are explicit;
Comfy Control never silently replaces a requested model with Grok. Passing a qualified
`provider/model` ID pins that provider; configured aliases such as `cliproxy` and
`runpod` are accepted.

```text
OpenAI-compatible client -> Comfy Control -> ComfyUI workers
                                      `-> CLI Proxy API (explicit Grok models)
```

The lightweight `control` image runs the control plane. The CUDA-enabled `worker`
image runs as a Pod or Serverless worker and contains pinned ComfyUI, workflows
and model profiles. RunPod and Vast.ai each have distinct Pod and Serverless
targets; Modal and SaladCloud use Serverless targets. The dashboard retains job
parameters and controller-owned input and output media. Its media library supports
fuzzy prompt lookup, structured parameter filters, sorting and navigable
source-to-derivative relationships.
The inference bearer key also authorises the sanitised status endpoint and confirmed
provider actions for the Comfy Workers Open WebUI Tool. An action additionally
requires the exact `x-comfy-control-action: provider/action` header.

Running control and worker services publish their current API descriptions at
`/openapi.json` and interactive documentation at `/docs`. Project-owned APIs are
unversioned; `/v1` is retained only for the OpenAI-compatible surface.

## Quick Start

On a container host:

```bash
cp .env.example .env
# Set the three blank bootstrap secrets.
docker compose up --build --detach
docker compose ps
```

Comfy Control listens on `http://localhost:28081`. Compose builds and starts only
the lightweight control plane. The control plane manages independently deployed
worker images.
Sign in to the model-first **Generate** studio. Use **Settings** to configure
provider credentials, deployment limits, model profiles, routing and worker
authentication. Saved settings are applied
without restarting the control plane.

## Repository Layout

- `catalogue/`: catalogue package, workflows, manifests and model profiles.
- `command/`: command-line entry point.
- `control/`: control-plane package and image build.
- `deploy/`: provider-owned deployment entry points.
- `docs/`: architecture, catalogue, development and deployment guidance.
- `providers/`: provider adapters and deployment request builders.
- `tests/`: behavioural tests grouped by runtime boundary.
- `worker/`: worker package, image build and upstream dependency constraints.

## Develop

```bash
mise run setup
mise run check
```

Use `mise run fmt` to format supported files. See:

- [Architecture](docs/architecture.md)
- [Catalogue & Models](docs/catalogue.md)
- [Deployment & Operations](docs/deployment.md)
- [Development](docs/development.md)

## Licence

Licensed under the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`). See [LICENSE](LICENSE).
