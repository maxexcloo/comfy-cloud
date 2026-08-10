# AGENTS.md

## Structure

- Keep controller and worker code in `src/comfy_control/` and tests in `tests/`.
- Keep detailed operational documentation in `docs/`; keep `README.md` focused on
  purpose, architecture and the shortest working setup.
- Keep only `AGENTS.md` and `README.md` as root Markdown files; put other project
  documentation in `docs/`.
- Keep pinned model sources in `profiles/`.
- Keep provider deployment assets grouped by provider in `deploy/`.
- Keep runtime configuration in `config/` and orchestration in root `compose.yaml`.
- Keep workflow manifests beside their API-format workflow JSON in `catalogue/`.
- Treat `config/control.yaml` as authoritative runtime configuration. Treat
  `config/bifrost.bootstrap.json` only as first-start input because Bifrost owns
  subsequent configuration in its SQLite store.

## Style

- Keep `main()` and execution guards last.
- Prefer direct code and standard tools over repository-specific abstractions.
- Preserve `LICENSE` and its legal text; never relicense without explicit approval.
- Sort mise tools and tasks, Prek hooks, Renovate rules, imports, and constants.
- Sort unordered peer entries by value shape: simple or single-line values first,
  then structured or multiline values, alphabetically within each group.
- Sort unordered peer headings, lists, and table rows alphabetically. Preserve
  narrative, procedural, dependency, interface, priority, and chronological order.
- Use `.yaml`, never `.yml`, for project-owned YAML files unless external tooling
  requires a fixed filename.
- Use Australian English throughout authored prose and every project-owned name,
  including identifiers, configuration keys, environment variables, paths, CLI
  commands, and options. Update every producer and consumer together; preserve only
  externally defined names and terminology.

## Verification

- Run `mise run check` before handoff.
