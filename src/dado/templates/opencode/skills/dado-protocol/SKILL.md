---
name: dado-protocol
description: DADO durable work protocol, artifact boundaries, state recovery, and role handoffs
---

# DADO operational protocol

## Sources of truth
- The active work directory plus `.dado/work/project/` are the only automatically relevant DADO artifacts.
- `request.md` preserves the original language and request. Append dated user clarifications; never rewrite original content.
- `work.yaml` contains stable `REQ-NNN` requirements, provenance (`user`, `discovered`, `inferred`), decisions, and plan approval. Task packets in `tasks/` contain dependencies, compact worker results and independent verdicts. Large logs, if needed, go in `evidence/`.
- Other work and `.dado/work/archive/` are cold storage. Never load automatically.

## Route a request
- For a new trivial, bounded edit, ask the user once whether to make a direct change or use tracked DADO work, unless they already chose. Selecting `dado-master` or `/dado` does not answer this question. Continue focused active work without creating another work.
- A direct edit goes to a worker with a concise scope and relevant checks, without work artifacts. A tracked small edit keeps normal approval and independent verification but usually needs only one requirement and one task; avoid unnecessary discovery. Larger or uncertain changes use the durable lifecycle below.
- Commit/push only on explicit user request. After a confirmed commit, link it to a confidently identified completed archive via `dado work link-commit <work-id> <hash>`; the archive never waits for a commit. Ask the historian only a targeted question about relevant archives. Git is the source of truth for actual committed files.

## Lifecycle
1. Capture request and preliminary understanding.
2. Run targeted explorer/researcher discovery appropriate to task size.
3. Consolidate the contract and DAG. Keep work units independently implementable and verifiable; avoid artificial microtasks.
4. Present a compact approval summary before executing if `approval.plan` is true.
5. Ask the deterministic CLI for ready tasks. Delegate only packets whose dependencies are done and whose scopes do not conflict.
6. Worker returns a compact Result Packet. Independent verifier evaluates original acceptance criteria and independently checks actual changed paths; a PASS must record these paths (even an empty list). Runtime transitions through review to done only after verification.
7. On ambiguity, failure, invalidation, scope expansion, or changed requirements stop and escalate. Invalidation marks dependent descendants stale.
8. On restart, run `dado resume`/`dado status`; reconcile uncertain running tasks conservatively, never repeat verified done tasks.
9. Validate requirements, explicitly promote durable project knowledge with provenance, create summary, and archive only when complete.
10. For a mistaken work, stop/reconcile running or reviewing tasks before archiving it as cancelled. Cancelled archives preserve the original request and do not imply completion.

## Context discipline
Keep packets and handoffs compact. Do not send large logs or unrelated history to the master. Persist useful evidence and refer to it. Never persist chain-of-thought. Human approval is a product boundary, not a formality.
