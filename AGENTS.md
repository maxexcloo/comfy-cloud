# AGENTS.md

## Structure

- Keep workflow manifests beside their API-format workflow JSON in `catalog/`.
- Keep provider deployment files in `deploy/`.
- Keep pinned model sources in `profiles/`.
- Keep gateway code in `src/comfy_cloud/` and tests in `tests/`.

## Style

- Prefer direct code and standard tools over repository-specific abstractions.
- Sort unordered peer entries by value shape: simple or single-line values first,
  then structured or multiline values, alphabetically within each group.
- Sort unordered peer headings, lists, and table rows alphabetically. Preserve
  narrative, procedural, dependency, interface, priority, and chronological order.
- Sort mise tools and tasks, Prek hooks, Renovate rules, imports, and constants.
- Keep `main()` and execution guards last.

## Verification

- Run `mise run check` before handoff.
