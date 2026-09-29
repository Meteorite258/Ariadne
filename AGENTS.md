# Ariadne Agent Instructions

Ariadne is a **downstream fork of Tau**, which is a Python implementation of Pi's minimalist coding-agent harness architecture. Ariadne reuses Tau's core and layers a long-running incident-investigation product on top. Develop it incrementally, with each phase clearly documented and tested.

## Fork Layout and Maintenance

Keep Tau's separation of concerns and add Ariadne in the outer layers:

```text
tau_ai            provider/model streaming layer              (Tau core)
tau_agent         portable agent harness, loop, tools, events (Tau core)
tau_coding        coding-agent CLI, resources, TUI, sessions  (Tau core + entry points)
tau_incident      incident domain: cases, evidence, planning,
                  execution, context, budgets/recovery, review,
                  reporting, memory
tau_coding/incident  incident application assembly: host, run queue,
                  daemon, alert intake, HTTP/RPC/TUI, telemetry,
                  Docker and OpenTelemetry adapters
```

Rules for the fork:

- `tau_ai` and `tau_agent` must stay free of incident concepts and of
  `tau_coding` / `tau_incident` imports. Changes there should stay generic and
  small enough to propose upstream.
- `tau_incident` must not import `tau_coding`; application wiring belongs in
  `tau_coding/incident`, not in the domain package.
- Put new Ariadne behavior in the outermost layer that can own it.
- Keep the distribution name `ariadne` and the CLI command `tau` consistent
  across `pyproject.toml`, `src/tau_coding/version.py`, and
  `src/tau_coding/update_check.py`.

## Incident Identifiers Are a Data Contract

The rename to Ariadne covers the product, the distribution, and the prose. It
deliberately does **not** cover the incident runtime identifiers:

- `AMADEUS_INCIDENT_TOKEN`, `AMADEUS_ALERT_TOKEN`, `AMADEUS_ALERT_TOKEN_FILE`,
  `AMADEUS_INCIDENT_CONFIG`, `AMADEUS_INCIDENT_MODE`, `AMADEUS_OUTPUT_FILES`,
  `AMADEUS_OUTPUT_BYTES`, `AMADEUS_VALIDATION_SECRET_KEY`
- the `amadeus-demo` telemetry/deployment environment, the `amadeus-analysis`
  image, and the `amadeus-*` service, tracer, history, and Alertmanager names

They are embedded in persisted case records and artifact hashes, in exported
offline datasets, in `versions.json` configuration digests, and in the Stage 7
verification fingerprints. Renaming them is not a text replacement: it requires
a migration that recomputes `receipts.content_hash` and `artifacts.sha256`,
rewrites exported datasets, restarts the demo so telemetry carries the new
environment tag, and re-runs the affected verification. Do not "finish" the
rename in these values without that migration.

A related trap: the case store is sharded by a digest of the **canonical project
path** (`TauPaths.project_incident_dir`). Moving or renaming the local checkout
directory changes that digest, so existing cases appear empty even though the
data is intact. Prefer keeping the checkout path stable, or migrate the
`incidents/` directory alongside the move.

## Upstream Sync

Ariadne tracks Tau upstream at <https://github.com/huggingface/tau>. Add it as a
fetch-only remote and review divergence before syncing:

```bash
git remote add upstream https://github.com/huggingface/tau.git
git fetch upstream
git log --oneline HEAD..upstream/main
```

Prefer rebasing the small, generic core patches over large merges, and keep
Ariadne work in separate commits so core changes stay easy to cherry-pick.

## Project Roadmap

Ariadne work is tracked in this repository's issues:

- <https://github.com/Meteorite258/Ariadne/issues>

Tau's upstream roadmap still governs phase ordering and architectural intent for
the reusable core:

- <https://github.com/huggingface/tau/issues/1>

## Architecture Principles

Preserve Pi's core separation of concerns:

```text
AgentHarness = reusable agent brain
AgentSession = coding-agent environment
TUI = one possible frontend
```

Tau should be organized around these layers:

```text
tau_ai      provider/model streaming layer
tau_agent   portable agent harness, loop, tools, events, sessions
tau_coding  CLI app, resources, skills, extensions, commands, TUI integration
```

Keep the core agent package independent of CLI, Textual, Rich rendering, session file locations, and application-specific resource loading.

## TUI Direction

Use Textual for the full interactive TUI, but only behind an adapter boundary. The agent harness should emit events; UI layers should consume those events.

Early phases should prioritize:

1. print-mode CLI
2. Rich renderers
3. Textual interactive app

Do not let Textual become a dependency of the reusable agent harness.

## Development Workflow

- Work in small, documented phases.
- Keep changes aligned with the Ariadne issues; keep core changes aligned with Tau's upstream roadmap.
- Add or update docs when introducing architectural concepts.
- Add tests for behavior before expanding features.
- Run tests and Python commands through `uv` (for example, `uv run pytest` or `uv run python ...`) so they use the project environment.
- Prefer simple, explicit abstractions over framework-heavy designs.
- Keep commits atomic: one coherent feature, fix, docs update, refactor, or cleanup per commit.

## GitHub Issue and PR Formatting

- When creating or editing GitHub issues and pull requests from the CLI, write multiline Markdown bodies through a temporary file or heredoc and pass them with `--body-file`.
- Do not pass escaped newlines like `\n` inside quoted `--body` strings; GitHub will render them literally instead of as line breaks.
- Use Markdown headings, blank lines, bullets, and backticks for commands/paths so issue and PR descriptions are readable.
- After creating or editing a GitHub issue or PR body, verify the rendered source with `gh issue view ... --json body` or `gh pr view ... --json body` when practical.

## Python Guidelines

- Target the Python version declared in `pyproject.toml`.
- Prefer typed dataclasses or schema models for core messages, events, tools, and sessions.
- Keep async boundaries explicit.
- Use fake providers and fake tools for deterministic agent-loop tests.
- Avoid provider-specific assumptions in core agent code.

## Documentation Expectations

Each substantial phase should leave behind beginner-friendly notes under `dev-notes/` (build journals, design docs, ADRs), explaining:

- what was added
- why it exists
- how it maps to Pi's design
- how to test or use it

When a phase adds or changes user-facing behavior, also update the docs under
`website/content/`. Note that Ariadne does not publish this site; the
`website/content/` guides are inherited from Tau and are kept for local preview
and future use.

