# DADO protocol

## Lifecycle

1. `dado work create` stores the literal original user request and initial metadata.
2. Master records a preliminary understanding; targeted repository/external discovery is delegated to explorer/researcher. Discovery intensity scales with complexity.
3. Master writes concise stable `REQ-NNN` requirements with provenance and acceptance criteria, plus decisions and a DAG of self-contained task packets.
4. Set work state to `awaiting_approval`; present the human a short goal/requirements/decisions/tasks/dependencies/scope/risk summary. Default `approval.plan: true` means execution must wait for explicit approval. After the user approves, record it with `dado work approve <id> --note "..."`; the runtime fingerprints request/spec/decision/task contract artifacts, so any later changes invalidate approval. `false` is appropriate only for trusted autonomous workflows.
5. Master asks runtime for ready tasks. It transitions selected task `pending → ready → running`. Scope conflicts are serialized. Worker returns a structured result; result means `review`, not done.
6. Independent verifier receives task criteria and relevant evidence/diff, then returns PASS/FAIL/BLOCKED. Record via `dado task verify`. Only PASS makes task done.
7. Failure, block, scope expansion, contradiction, or changed premise halts affected execution. `dado task invalidate` marks the target and all descendants stale for master re-planning.
8. After all requirements and tasks are satisfied, master may explicitly promote verified durable knowledge; summary is generated and work is moved to `archive/<year>/`.

## Role handoff

Explorer results cite evidence and uncertainty, not just file names. Worker packets contain task goal, requirement links, scope, dependency, acceptance, verification and risk. Worker cannot edit specs/decisions or unilaterally expand scope. Verifier is independent and read-only. Results stay compact; logs go to evidence. No internal chain-of-thought is persisted.

## States

Task states: `pending`, `ready`, `running`, `review`, `done`, `blocked`, `failed`, `stale`, `cancelled`. Valid transitions are explicit in `domain.py`. `done` is exclusively assigned after verifier PASS. Failed/blocked tasks can be made ready after a deliberate retry decision. `done → stale` is allowed for premise invalidation. Cancelled is terminal. A DAG may not run a task before all dependencies are done.

The approval setting is present in `.dado/config.yaml`; OpenCode does not provide a dedicated durable human-approval protocol for a DADO plan, so the primary agent must pause and ask the user rather than infer approval.
