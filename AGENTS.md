# AGENTS.md

## Structure

- Keep gateway code in `src/comfy_cloud/` and tests in `tests/`.
- Keep provider deployment files in `deploy/`.
- Keep workflow manifests beside their API-format workflow JSON in `catalog/`.
- Keep pinned model sources in `profiles/`.

## Style

- Prefer direct code and standard tools over repository-specific abstractions.
- Sort mise tools and tasks, Prek hooks, Renovate rules, imports, and constants.
- Keep `main()` and execution guards last.

## Verification

- Run `mise run check` before handoff.
