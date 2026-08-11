# Comfy Control

Comfy Control is an authenticated OpenAI-compatible control plane for ComfyUI. It
routes image and video requests across Pod and Serverless deployments, controls
provider lifecycles, reports usage and credit, and falls back to CLIProxyAPI.
Public model IDs use configured provider fallback. Passing a qualified
`provider/model` ID pins that provider; configured aliases such as `cliproxy` and
`runpod` are accepted.

```text
OpenAI-compatible client -> Comfy Control -> ComfyUI workers
                                      `-> CLIProxyAPI (fallback)
```

The lightweight `control` image runs the control plane. The CUDA-enabled `worker`
image runs as a Pod or Serverless worker and contains pinned ComfyUI, workflows
and model profiles. RunPod and Vast.ai each have distinct Pod and Serverless
targets; Modal and SaladCloud use Serverless targets. The dashboard retains job
parameters and controller-owned output media with an authenticated popup viewer.
The inference bearer key also authorises the sanitised status endpoint and confirmed
provider actions for the Comfy Workers Open WebUI Tool. An action additionally
requires the exact `x-comfy-control-action: provider/action` header.

## Quick Start

On a container host:

```bash
cp .env.example .env
# Set the required control, worker and UI secrets.
docker compose up --build --detach
docker compose ps
```

Comfy Control listens on `http://localhost:28081`. Compose builds and starts only
the lightweight control plane. The control plane manages independently deployed
worker images; `LOCAL_POD_URL` may point at an externally managed Pod worker.

## Repository Layout

- `catalogue/`: checksum-pinned API-format ComfyUI workflows and manifests.
- `deploy/`: provider-specific Comfy Control deployment assets.
- `docs/`: architecture, catalogue, development and deployment guidance.
- `profiles/`: pinned model sources.
- `src/`: Comfy Control implementation.
- `tests/`: behavioural and repository tests.

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
