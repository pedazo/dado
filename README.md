# DADO — Durable Artifact-Driven Orchestration

**Context is disposable. Work is durable.** DADO adds a small, local deterministic runtime and role prompts on top of OpenCode. YAML/Markdown artifacts—not conversation history—are the source of truth. There is no DADO telemetry and no external runtime service.

## Install

Requires Python 3.10+ and OpenCode V2 (the implementation was checked against OpenCode `v2.0.16`). Install globally with `pipx install .` from this checkout, or use `scripts/install.sh` / `scripts/install.ps1` (which creates an isolated venv when pipx is unavailable). If you manage your own virtual environment, activate it and use `python -m pip install .`.

```sh
cd /path/to/project
dado init
dado models
opencode
```

Use `/dado <request>` or select `dado-master`. `dado init` is project-local and non-destructive; global CLI installation alone does not change projects. Unix bootstrap: `scripts/install.sh`; PowerShell: `scripts/install.ps1`. For development, `python -m pip install -e '.[dev]'` (test dependency: `pytest`). Upgrade from a checkout with `python -m pip install --upgrade .`, then `dado update`. Changed user-customized templates are preserved and reported. See [installation and removal](docs/opencode-integration.md).

## Quickstart

```sh
dado init
dado models                         # query current `opencode models`
opencode                            # select dado-master
```

The master captures the original user request, performs targeted discovery through `dado-explorer` / `dado-researcher`, creates an appropriately sized spec and dependency DAG, asks for plan approval by default, then delegates bounded packets to workers and independent verification. The runtime—not an LLM—validates transitions, cycles, readiness, scope claims, recovery, and archive prerequisites.

Useful local commands:

```sh
dado status
dado work list
dado work show WORK-ID
dado work ready WORK-ID
dado work focus WORK-ID
dado doctor
dado resume WORK-ID
dado validate
dado work archive WORK-ID
```

The runtime also exposes structured `work create/state/approve`, `task add/state/result/verify/invalidate`, `requirement add/state`, `decision add`, and `promote` commands for offline operation and agent handoffs. `dado task verify` is the only way a task becomes done.

## Artifacts and context boundaries

`.dado/` contains config, project memory, active work and cold archive. One active work never implicitly reads other work or the archive. Keep structured plans/results/summaries under version control; `.dado/.gitignore` ignores large logs and temporary evidence. Project memory is explicitly promoted with provenance. Work archive is never auto-loaded.

## Recovery and acceptance workflow

`dado resume <id>` rebuilds from artifacts. A task left `running` without a durable result is uncertain and returned to `ready` (or `pending` if dependencies are not done); it is never assumed complete. A task in `review` is completed only if independent PASS evidence was persisted. Previously verified tasks remain done. The automated interrupted-run acceptance is covered by `tests/test_runtime.py::test_interrupted_work_recovery_full_acceptance_and_archive`; see [recovery](docs/recovery.md).

## Docs

- [Architecture](docs/architecture.md)
- [Protocol](docs/protocol.md)
- [Artifacts and schemas](docs/artifacts.md)
- [OpenCode integration](docs/opencode-integration.md)
- [Recovery](docs/recovery.md)
- [Architecture decisions](docs/adr/)

## Limitations

DADO provides deterministic local state/runtime, native OpenCode role definitions and a command entry point. It does not silently launch or schedule LLM subprocesses: delegation remains OpenCode's native agent/task interaction, with deterministic CLI state handoffs. Scope enforcement validates worker-reported changed paths; it is not an OS sandbox and cannot prevent a malicious/buggy worker from writing outside scope before reporting. Use OpenCode permissions, Git review, and project-specific shell policy as additional safeguards.
