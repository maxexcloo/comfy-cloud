# Comfy Control

Comfy Control is an authenticated OpenAI-compatible media gateway. It translates
portable image and video requests into checksum-pinned ComfyUI workflows, exposes
health, metrics and native ComfyUI proxy routes, and can fail over to CLIProxyAPI.

```text
OpenAI-compatible client -> Comfy Control -> ComfyUI (primary)
                                      `-> CLIProxyAPI (fallback)
```

ComfyUI is always the primary media runtime. When configured, CLIProxyAPI provides
fixed Grok fallbacks for image generation, image editing and video generation.
Deployment selection and infrastructure lifecycle remain external concerns.

## Quick Start

On a host with the NVIDIA Container Toolkit:

```bash
cp .env.example .env
# Replace API_KEY and COMFY_UI_PASSWORD. FLUX.2 klein 9B is selected by default.
docker compose up --build --detach
docker compose ps
```

Comfy Control listens on `http://localhost:28081`. The image starts
`comfy-control pod` by default, exposing the ComfyUI frontend with basic
authentication. Start `comfy-control serverless` instead to expose APIs without
the frontend.

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
