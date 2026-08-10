# AI Router

AI Router is a personal inference gateway for language, image and video models. It
combines Bifrost, CLIProxyAPI and a project-owned Comfy Control service behind one
authenticated OpenAI-compatible endpoint.

```text
                         +-> CLIProxyAPI -> subscription-backed LLMs
Open WebUI -> Bifrost ---+
                         +-> Comfy Control -> on-demand ComfyUI workers
```

Bifrost owns authentication, language-model routing and request logs. CLIProxyAPI
adapts authenticated CLI subscriptions. Comfy Control owns media routing, provider
lifecycle, durable video jobs and the worker-side OpenAI/ComfyUI gateway.

## Quick Start

```bash
cp .env.example .env
# Replace the core placeholders; configure provider values when enabling media.
docker compose up --build --detach
docker compose ps
```

The standalone endpoints default to:

| Service                | URL                      |
| ---------------------- | ------------------------ |
| Bifrost gateway/UI     | `http://localhost:28080` |
| CLIProxyAPI management | `http://localhost:28317` |
| Comfy Control UI/API   | `http://localhost:28081` |

[`config/bifrost.bootstrap.json`](config/bifrost.bootstrap.json) seeds a new
Bifrost data volume once; Bifrost owns later changes in SQLite. The mounted
[`config/control.yaml`](config/control.yaml) remains authoritative on every Comfy
Control start. Copy routes from
[`config/control.example.yaml`](config/control.example.yaml) before expecting media
models to appear. Configured provider deploy, status, start, stop and destroy
actions appear in the authenticated Comfy Control UI.

## Repository Layout

- `catalogue/`: checksum-pinned API-format ComfyUI workflows and manifests.
- `config/`: standalone runtime configuration and examples.
- `deploy/`: provider-specific deployment assets.
- `docs/`: architecture, catalogue, development and deployment guidance.
- `profiles/`: pinned model sources.
- `scripts/`: small operational checks.
- `src/comfy_control/`: controller and worker implementation.
- `tests/`: behavioural and repository tests.

The overall product is AI Router. `comfy-control` remains the narrower Python
package, command and service name.

## Develop

```bash
mise run setup
mise run check
```

Use `mise run fmt` to format supported files and `mise tasks` to list deployment
operations. See:

- [Architecture](docs/architecture.md)
- [Catalogue & Models](docs/catalogue.md)
- [Deployment & Operations](docs/deployment.md)
- [Development](docs/development.md)

## Licence

Licensed under the GNU Affero General Public License v3.0 only
(`AGPL-3.0-only`). See [LICENSE](LICENSE).
