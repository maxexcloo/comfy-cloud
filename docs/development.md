# Development

## Setup

```bash
mise run setup
mise run check
```

Setup creates `.mise.local.toml` from its template when missing and installs Prek
hooks. Local secrets and provider identifiers remain gitignored. Checks use an
isolated Python environment; a repository-local `.venv` is not required.

Use `mise run fmt` for supported formatting and `mise run cleanup` to remove test,
lint and bytecode caches. Run `mise tasks` for the full provider control surface.
Use `mise run gateway:check` for an opt-in live Bifrost discovery and completion
probe.

## Checks

`mise run check` runs repository hygiene, structured-file validation, GitHub
Actions linting, Dockerfile linting, formatting, Python linting and the pytest
suite. Run it before handoff.

The container workflow separately builds controller and worker images, validates
the Compose model and performs readiness smoke tests for both images on pull
requests.

## Project Boundaries

- Keep deterministic workflow selection in `catalogue/`, not prompt prose or
  provider deployment code.
- Keep external field names unchanged; use Australian English for project-owned
  prose and identifiers.
- Keep pinned weight sources in `profiles/`.
- Keep portable API behaviour in `src/comfy_control/`.
- Keep provider-specific assets grouped under `deploy/`.

## Command-Line Interface

The `comfy-control` command supports:

- `catalogue-list` and `repository-check`;
- `models-fetch`;
- `pack` and `unpack`;
- `workflow-add`.

Use `workflow-add` only with an API-format ComfyUI export and restart the worker
after changing its mounted catalogue.
