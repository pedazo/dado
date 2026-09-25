---
name: dado-protocol
description: DADO durable work protocol, artifact boundaries, state recovery, and role handoffs
---

# DADO operational protocol

## Sources of truth
- The active work directory plus `.dado/work/project/` are the only automatically relevant DADO artifacts.
- `request.md` preserves the original language and request. Append dated user clarifications; never rewrite original content.
- `spec.md` contains stable `REQ-NNN` requirements and provenance (`user`, `discovered`, `inferred`). `decisions.md` stores conclusions, concise rationale, alternatives, and impact—not hidden reasoning.
- `plan.yaml` is a DAG. Task packets are in `tasks/`, compact results in `results/`, potentially large logs in `evidence/`.
- Other work and `.dado/work/archive/` are cold storage. Never load automatically.

## Lifecycle
1. Capture request and preliminary understanding.
2. Run targeted explorer/researcher discovery appropriate to task size.
3. Consolidate the contract and DAG. Keep work units independently implementable and verifiable; avoid artificial microtasks.
4. Present a compact approval summary before executing if `approval.plan` is true.
5. Ask the deterministic CLI for ready tasks. Delegate only packets whose dependencies are done and whose scopes do not conflict.
6. Worker returns a compact Result Packet. Independent verifier evaluates original acceptance criteria; runtime transitions through review to done only after verification.
7. On ambiguity, failure, invalidation, scope expansion, or changed requirements stop and escalate. Invalidation marks dependent descendants stale.
8. On restart, run `dado resume`/`dado status`; reconcile uncertain running tasks conservatively, never repeat verified done tasks.
9. Validate requirements, explicitly promote durable project knowledge with provenance, create summary, and archive only when complete.

## Context discipline
Keep packets and handoffs compact. Do not send large logs or unrelated history to the master. Persist useful evidence and refer to it. Never persist chain-of-thought. Human approval is a product boundary, not a formality.
