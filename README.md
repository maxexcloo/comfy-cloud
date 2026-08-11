# Comfy Control

Comfy Control is an authenticated OpenAI-compatible gateway for ComfyUI. It runs
beside an unmodified ComfyUI process, translates portable image and video requests
into checksum-pinned API workflows, and exposes health, metrics and native ComfyUI
proxy routes.

```text
OpenAI-compatible client -> Comfy Control -> ComfyUI
```

Provider selection, failover and infrastructure lifecycle belong outside Comfy
Control. Run one instance for each independently managed ComfyUI worker and route
between those instances in an external gateway when required.

## Quick Start

On a host with the NVIDIA Container Toolkit:

```bash
cp .env.example .env
# Replace API_KEY and COMFY_UI_PASSWORD, then select MODEL_PROFILES as required.
docker compose up --build --detach
docker compose ps
```

Comfy Control listens on `http://localhost:28081`. `MODE=pod` exposes the ComfyUI
frontend with basic authentication; `MODE=serverless` exposes APIs without the
frontend.

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
