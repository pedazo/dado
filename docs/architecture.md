# Architecture

DADO consists of a Python CLI/runtime, human-readable artifacts, and an OpenCode adapter. Domain rules are independent of OpenCode. OpenCode owns agent invocation, model/provider authentication, permissions, tool execution, and its conversation UI. DADO owns durable work state, schema checks, DAG scheduling decisions, transition enforcement, event auditing, recovery, and archival.

```text
User → dado-master → focused explorer/researcher evidence → spec + DAG
                                                   ↓ approval
                  deterministic ready queue → worker → independent verifier
                                      ↑             ↓ result/evidence
                                      └── runtime state + event log
```

Only `dado-master` is primary; all other roles are subagents. The roles are intentionally few: orchestration/planning belongs to master, bounded implementation to worker, verification to verifier, local discovery to explorer, external references to researcher, and explicit archive lookup to historian. Shared protocol detail is loaded from one skill instead of copied into prompts.

Runtime modules: `domain.py` (DAG/transitions), `store.py` (state/verification/recovery/archive), `fs.py` (atomic writes/lock/event append), `models.py` (OpenCode model adapter), `installer.py` (managed-file ownership), and `cli.py`. YAML artifacts are validated by JSON Schema at runtime. `events.jsonl` is append-only audit/diagnostic data, not the primary state source.

## Scheduler and concurrency

`work ready` calculates tasks whose dependencies are done, bounded by the configured parallelism. OpenCode currently provides subagent invocation, not a DADO-specific durable scheduler API; the master uses deterministic CLI results to select work. Exact file/directory scopes are compared; any glob scope conservatively serializes against another task because glob intersection is difficult to prove. Writes use temp + atomic rename; state-changing operations share a local lock. This is local single-project coordination, not distributed locking.

## Security boundaries

Role permissions express least privilege as supported by current OpenCode V2. They are not an OS sandbox. Worker result paths are checked against task scope, but pre-write enforcement is not guaranteed. CLI file operations reject work paths escaping the active directory. No telemetry is implemented. OpenCode itself may use configured providers/services under the user's own OpenCode settings.

## Context / cold storage

The focused work folder and project memory are explicitly named to the master. Active peers and archive are not listed as ambient context; archive lookup requires a historian request. Large command output belongs in evidence and is referenced by path. The original request language is preserved.
