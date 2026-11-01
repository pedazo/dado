# Runtime operations and evidence

## Layout and migration

Fresh projects use layout v2. The versioned `.dado/layout.yaml` contains a shared project UUID; `.dado/config.yaml` holds shared policy, while ignored `.dado/config.local.yaml` holds `profiles` and `active_profile`. `.dado/identity` is a per-checkout UUID. Active/archive work, index, event log and local operational state are ignored individually; only explicitly promoted material under `.dado/work/project/` is intended for sharing. DADO extends `.dado/.gitignore` without removing existing lines, untracking files, or rewriting tracked operational files. It does not modify tracked OpenCode agents for local profile preferences.

Projects without a layout marker continue using legacy `.dado/config.yaml`. Inspect migration before applying it; migration backs up the old config and reports tracked-file considerations:

```sh
dado migrate --project .
dado migrate --project . --apply
```

`dado repair` is report-only by default. `dado repair --apply` replays pending transaction journals; it does not roll back changes. A `building` journal is inspection-only, not replayable. Inspect unreadable or unknown journals manually. Transactions are local filesystem journals, not a distributed transaction or distributed lock.

## Host model overlay

In layout v2, local profiles never need to alter shared agent definitions. Use `dado models` and `dado profiles` for profile selection, `dado host check` for a read-only probe of the actual host, and `dado host exec -- ...` to execute OpenCode with the local overlay. For example:

```sh
dado host check
dado host exec -- run "<request>"
```

`inherit` means the shared host/project default. Profile changes require a new `dado host exec` invocation; existing private servers retain their inline overlay. A host check is a configuration probe, not a model call.

## Captured verification

`dado check` requires an explicit command after `--`. It captures output/exit status in the work's `evidence/checks/` and associates it with exact acceptance text. For task acceptance:

```sh
dado check WORK --task T --criterion 'exact acceptance' -- python -m pytest
dado task verify WORK T PASS --check 'independent review' --paths-checked --changed src/example.py --record evidence/checks/RECORD.json
```

For work-level requirements, criteria are spelled `REQ-NNN: criterion`:

```sh
dado check WORK --criterion 'REQ-001: exact acceptance' -- python -m pytest
dado work verify WORK --record evidence/checks/RECORD.json
```

The task-level `--record` option may be repeated; each work-level `--record` may also be repeated. Captured records must belong to that work/project, have exit code zero, map to exact contract criteria, and remain current. Work-level verification requires all tasks done and covers all non-waived requirement acceptance criteria. The independent verifier still owns the verdict; captured execution does not itself prove a requirement or establish trusted session identity. Verification snapshots bind PASS to current scoped code: if code changes, repeat checks and independent review. `verification.require_captured` and `verification.require_integration` are policy settings; fresh layout-v2 defaults require integration evidence, while legacy defaults are unchanged. Existing legacy declared evidence does not become captured evidence merely by migration.

Glob scopes and not-yet-existing paths bind to their nearest existing literal ancestor for verification. This deliberately conservative fingerprint catches later matching additions; it may also require a recheck after an unrelated change under that ancestor.

## Budgets and advisory policy

Budget files are opt-in per work. Commands are `dado budget init WORK`, `reserve WORK --task TASK --cost AMOUNT`, `charge WORK RESERVATION --cost AMOUNT`, `release WORK RESERVATION`, and `status WORK`; init accepts caps via `--cost`, `--minutes`, and `--retries`. If a ledger exists, task start requires its reservation. Costs are finite admission estimates against configured caps—not live provider prices or billing records. Specify known estimates and release reservations intentionally.

`dado policy decide` is an optional advisory explanation of routing/budget policy. It does not launch LLMs. The evaluation report command is:

```sh
dado evaluate report --project . --registry evaluations/registry.yaml \
  --runs evaluations/runs --out /path/to/report.json
```

Repository fixtures marked synthetic are illustrative test data, not measured model quality, speed, or cost. External run records may be evaluated when they carry provenance. Cost per accepted task is total incurred cost, including failed attempts, divided by the number accepted. Unknown costs remain unknown. The report is not evidence of comparative superiority or a guarantee of future outcomes.

## Local isolation

Isolation operates on a declared local snapshot scope and does not require Git. Dirty and untracked files in scope are included; `.dado`, Git metadata, common caches and secret-looking paths are excluded or refused. Snapshots stay under `.dado/local/isolation/` by default.

```sh
dado isolate create --project . --scope src
dado isolate list --project .
dado isolate integrate --project . --snapshot EXECUTION_ID
```

Integration only copies changes within the snapshot scope (an optional `--scope` narrows it). Conflicts make no changes and preserve the snapshot for resolution. Interrupted integration can be continued with `--resume`; do not describe this as rollback. No commits or pushes occur. After files are integrated, run final checks and required independent verification. Local resource reservations coordinate only this checkout and are not distributed locks.

## Portable handoffs and CI

`dado handoff export WORK file.json [--evidence evidence/relative-path]` exports a portable handoff; `dado handoff import file.json` creates a new local execution. Imported approval and verification are historical only: re-review the contract, approve, and verify locally. Free-text review notes and exported evidence can contain confidential material; inspect before sharing and use only relative evidence paths.

CI configuration is provided for Linux and Windows with Python 3.10 and 3.13. This describes configured jobs, not a claim that remote CI ran successfully for a particular change.
