# AGENTS.md

## Structure

- Keep workflow manifests beside their API-format workflow JSON in `catalogue/`.
- Keep provider deployment files in `deploy/`.
- Keep pinned model sources in `profiles/`.
- Keep standalone stack configuration in `standalone/` and orchestration in root `compose.yaml`.
- Keep controller and worker code in `src/comfy_control/` and tests in `tests/`.

## Style

- Prefer direct code and standard tools over repository-specific abstractions.
- Sort unordered peer entries by value shape: simple or single-line values first,
  then structured or multiline values, alphabetically within each group.
- Sort unordered peer headings, lists, and table rows alphabetically. Preserve
  narrative, procedural, dependency, interface, priority, and chronological order.
- Sort mise tools and tasks, Prek hooks, Renovate rules, imports, and constants.
- Keep `main()` and execution guards last.
- Preserve `LICENSE` and its legal text; never relicense without explicit approval.
- Use Australian English throughout authored prose and every project-owned name,
  including identifiers, configuration keys, environment variables, paths, CLI
  commands, and options. Update every producer and consumer together; preserve only
  externally defined names and terminology.

## Verification

- Run `mise run check` before handoff.
