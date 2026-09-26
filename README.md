# DADO — Durable Artifact-Driven Orchestration

**Context is disposable. Work is durable.** DADO organizes OpenCode agents into a resumable workflow that records progress and verifies changes. Use less expensive models for bounded tasks and reserve more capable models for planning or review to help control costs without giving up oversight. DADO uses a small local runtime and durable YAML/Markdown artifacts—not conversation history—as the source of truth. There is no DADO telemetry or external runtime service.

## Install

Requires Python 3.10+ and OpenCode V2 (the implementation was checked against OpenCode `v2.0.16`). Install globally with `pipx install .` from this checkout, or use `scripts/install.sh` / `scripts/install.ps1` (which creates an isolated venv when pipx is unavailable). If you manage your own virtual environment, activate it and use `python -m pip install .`.

```sh
cd /path/to/project
dado init
opencode
```

In OpenCode, run `/dado <request>` or select `dado-master` and describe your request. `dado init` is project-local and non-destructive; installing the CLI alone does not change projects. For development, use `python -m pip install -e '.[dev]'`. See [installation, updates and removal](docs/opencode-integration.md).

## How to use DADO

```text
You → /dado or dado-master
           ├─ Trivial edit: ask direct or tracked, unless you already chose
           │    ├─ Direct → worker → relevant checks → report
           │    └─ Tracked → compact one-task DADO work (normal approval + verification)
           ├─ Explicit one-off Git operation → worker → report; optionally link a confirmed commit
           └─ Durable work (feature, fix, review, research, documentation):
                capture request → targeted discovery → requirements + task DAG
                → your plan approval (default) → ready tasks → worker(s)
                → independent verifier → requirements checked → summary + archive
```

The **master** coordinates and talks to you. It can ask an **explorer** about the repository, a **researcher** about external references, and a **historian** about a specific previous work when needed. **Workers** perform bounded tasks; an independent **verifier** checks each completed task against its original criteria and actual changed paths. OpenCode invokes the agents; the `dado` CLI validates durable state and task readiness. The master does not need general Git shell access.

For example, ask `Fix the login timeout`, `Review the authentication flow for bugs`, or `Correct this typo`. You can speak directly to a selected `dado-master`; `/dado` is only a shortcut and does not force the full protocol. For the typo, the master asks which route you prefer unless you already specified it.

### Choose the path for your request

| Situation | What happens / what to do |
|---|---|
| Trivial edit with no stated preference | The master asks once: do it directly, or keep it as tracked DADO work? You can include your choice in the initial request to skip the question. |
| Trivial edit you want in DADO history | A compact plan usually has one requirement and one task. Approval and independent verification still apply; discovery is only as extensive as needed. |
| Trivial edit you want immediately | The master gives a bounded request to a worker, who runs relevant checks and reports back without creating a DADO work. |
| New feature, bug fix, refactor or documentation change | Describe the goal with `/dado <request>`. The master captures the original request, investigates only what is relevant, and proposes requirements and one or more tasks. |
| Code review, investigation or research without edits | Ask for the review or question. Discovery is targeted; if the result should be durable, it follows the same tracked-work lifecycle. A task can finish with no changed files. |
| Plan needs adjustment | Clarify requirements before approving. By default, the master waits for your explicit approval; it records it with `dado work approve WORK-ID --note "..."`. A later contract change requires fresh approval, and an affected task must be rerun. |
| Several independent tasks | The master requests ready tasks with `dado work ready WORK-ID`. Dependencies, configured worker capacity and overlapping file scopes determine what can run together. |
| Worker is blocked, fails or needs broader scope | The master stops the affected work, resolves the issue with you, then retries or revises the plan. A changed premise can invalidate a task and its dependents via `dado task invalidate WORK-ID T-001 "reason"`. |
| Verification fails | A completed worker result enters `review`, **not** `done`. The independent verifier returns PASS, FAIL or BLOCKED; only a recorded PASS can complete the task. |
| Session or process is interrupted | Run `dado status`, then `dado resume WORK-ID`. Verified tasks remain done; uncertain running tasks are offered for inspection/retry, and tasks already in review need only verification. Inspect partial source edits before retrying. |
| Mistaken or abandoned work | Cancel it, stop/reconcile any running or reviewing tasks, then archive it. A cancelled archive retains the original request without claiming completion. |
| One-off Git operation (for example, commit/push) | Ask explicitly; completing or archiving work never commits automatically. The worker checks Git state and commits only the requested changes. For confidently related completed archives, the master can then link the confirmed commit hash; uncertain attribution requires clarification. |
| Several active works or previous context | Use `dado work list` and `dado work focus WORK-ID` to select the intended work. Other active works and the archive are not loaded automatically; the historian consults past summaries only for a concrete question. |
| Work is complete | The master checks tasks and requirements, optionally promotes sourced project knowledge, then archives completed work (unless configured to leave it active). Archived work has a summary for later lookup. |

### Configure models and operate locally

```sh
dado models                               # choose a model and optional variant per agent
dado models --model provider/model --variant high --roles dado-worker
dado models --variant none --roles dado-worker  # clear a variant
dado status                               # focused work and next tasks
dado work list                            # all active works
dado work show WORK-ID                    # work contract and task packets
dado work focus WORK-ID                   # switch focus explicitly
dado work ready WORK-ID                   # runnable tasks
dado resume WORK-ID                       # reconcile an interrupted run
dado doctor                               # installation and state diagnostics
dado validate                             # validate active task graphs
dado work link-commit WORK-ID GIT-SHA     # associate a confirmed commit with a completed archive
```

Variants are model-specific (for example, reasoning effort); OpenCode determines which variant IDs a model supports. Model and variant selection live in agent frontmatter, not in another DADO registry. `dado` also exposes `work create/state/approve/archive`, `task add/state/result/verify/invalidate`, `requirement add/state`, `decision add` and `promote` for manual operation and agent handoffs. `task add` and `task result` accept `-` to read YAML from stdin instead of creating an extra packet file.

For a manual verification handoff, `dado task verify WORK-ID T-001 PASS --check "..." --paths-checked --changed path/to/file` records an independently checked path; omit `--changed` when there were no file changes. See the [full protocol](docs/protocol.md) for the state transitions and approval rules.

## Artifacts and context boundaries

`.dado/` contains config, project memory, active work and cold archive. One active work never implicitly reads other work or the archive. Each new tracked work has `request.md`, `work.yaml`, and one packet per task containing its contract, worker result and independent verdict. A completed archive can also record optional `related_commits` in `work.yaml`, including commits requested after archival. Git (`git show GIT-SHA`) remains the source of truth for the exact contents of a commit; association does not gate archival. The host project decides whether to version-control its DADO data. Project memory is explicitly promoted with provenance. Work archive is never auto-loaded.

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

DADO provides deterministic local state/runtime, native OpenCode role definitions and a command entry point. It does not silently launch or schedule LLM subprocesses: delegation remains OpenCode's native agent/task interaction, with deterministic CLI state handoffs. Scope enforcement checks declared and independently reported paths; it is not an OS sandbox and cannot prevent changes outside scope before verification. Archive moves and index updates are not a multi-file transaction; interrupted archival may need manual reconciliation. See [recovery](docs/recovery.md).
