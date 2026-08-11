# Development

## Setup

```bash
mise run setup
mise run check
```

Setup creates `.env` from its template when missing and installs Prek hooks. Checks
use an isolated Python environment; a repository-local `.venv` is not required.

Use `mise run fmt` for supported formatting and `mise run cleanup` to remove test,
lint and bytecode caches.

## Checks

`mise run check` runs repository hygiene, structured-file validation, GitHub
Actions linting, Dockerfile linting, formatting, Python linting and the pytest
suite. Run it before handoff.

The container workflow separately builds and smoke-tests the Comfy Control image.

## Project Boundaries

- Keep deterministic workflow selection in `catalogue/`, not prompt prose or
  deployment code.
- Keep external field names unchanged; use Australian English for project-owned
  prose and identifiers.
- Keep pinned weight sources in `profiles/`.
- Keep portable API behaviour directly in `src/`.
- Keep provider-specific deployment assets in `deploy/`.
- Keep provider selection, failover and lifecycle orchestration outside this
  repository.

## Command-Line Interface

The `comfy-control` command supports:

- `catalogue-list` and `repository-check`;
- `models-fetch`;
- `pack` and `unpack`;
- `worker`;
- `workflow-add`.

Use `workflow-add` only with an API-format ComfyUI export and restart Comfy Control
after changing its mounted catalogue.
