# Comfy Control

Comfy Control is an authenticated OpenAI-compatible control plane for ComfyUI. It
routes image and video requests across Pod and Serverless deployments, controls
provider lifecycles, reports usage and credit, and falls back to CLIProxyAPI.

```text
OpenAI-compatible client -> Comfy Control -> ComfyUI workers
                                      `-> CLIProxyAPI (fallback)
```

The same image runs as the control plane, a Pod worker or a Serverless worker.
RunPod and Vast.ai each have distinct Pod and Serverless targets; Modal and
SaladCloud use Serverless targets. The dashboard retains job parameters and
controller-owned output media with an authenticated popup viewer.

## Quick Start

On a host with the NVIDIA Container Toolkit:

```bash
cp .env.example .env
# Set the required control, worker and UI secrets. FLUX.2 klein 9B is the default.
docker compose up --build --detach
docker compose ps
```

Comfy Control listens on `http://localhost:28081`. Compose starts the control
plane and one local Pod worker. The image supports `control`, `pod`,
`serverless` and `vast-serverless` commands.

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
