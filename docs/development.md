# Development

## Setup

```bash
mise run setup
mise run check
```

Setup creates the bootstrap `.env` from its template when missing and installs Prek
hooks. Set the three blank secrets before starting Compose. Checks use an isolated
Python environment; a repository-local `.venv` is not required.

Use `mise run fmt` for supported formatting and `mise run cleanup` to remove test,
lint and bytecode caches.

## Checks

`mise run check` runs repository hygiene, structured-file validation, GitHub
Actions linting, Dockerfile linting, formatting, Python linting and the pytest
suite on the latest Python version pinned by Mise. Run it before handoff.

The container workflow separately builds and smoke-tests the Comfy Control image.

## Project Boundaries

- Keep deterministic workflow selection in `catalogue/`, not prompt prose or
  deployment code.
- Keep external field names unchanged; use Australian English for project-owned
  prose and identifiers.
- Keep pinned weight sources in `catalogue/profiles/`.
- Keep catalogue behaviour and data together in `catalogue/`.
- Keep command-line entry points in `command/`.
- Keep control-plane code and its image build in `control/`.
- Keep provider-owned deployment entry points in `deploy/`.
- Keep provider behaviour in `providers/`.
- Keep built-in provider capabilities, lifecycle and telemetry in the provider
  registry and adapters.
- Keep each provider's API discovery, status and telemetry in its own
  `providers/<name>.py` module.
- Keep safe user-editable preferences in `ControlPreferences` and SQLite.
- Keep tests grouped by the same catalogue, control, provider and worker boundaries.
- Keep worker code and its image build in `worker/`.
- Keep OpenAI-compatible routing in the controller and canonical execution in workers.

## Command-Line Interface

The `comfy-control` command supports:

- `catalogue-list` and `repository-check`;
- `control`;
- `models-fetch`;
- `pack` and `unpack`;
- `pod` and `serverless` runtime services;
- `vast-serverless`;
- `workflow-add`.

Use `workflow-add` only with an API-format ComfyUI export and restart Comfy Control
after changing its mounted catalogue.

## API Contracts

Running control and worker services are the source of truth for API contracts.
Read `/openapi.json` for machine consumption or `/docs` for the interactive view.
Do not commit generated OpenAPI snapshots or add project-owned version prefixes.
