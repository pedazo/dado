# Artifact reference

```text
.dado/
  config.yaml                 # DADO-only policy; no duplicate OpenCode model config
  managed-files.json          # ownership hashes for non-destructive updates
  work/
    index.yaml                # tiny active-work discovery index and focused ID
    project/{architecture,conventions,constraints,decisions}.md
    events.jsonl              # append-only state audit
    active/<work-id>/
      work.yaml request.md spec.md decisions.md plan.yaml
      tasks/T-NNN.yaml results/T-NNN.yaml verification/T-NNN.yaml evidence/
    archive/<year>/<work-id>/  # same artifacts plus summary.md
```

`work.yaml` carries requirement IDs/status/provenance, work state, plan approval state and Git base/branch/final commit. `approval.yaml` records the user's explicit approval and a fingerprint of the request/spec/task contract (mutable status fields excluded), so contract edits require reapproval. Decisions are auditable but adding a rationale alone does not force reapproval; if it changes the contract, revise the spec/task packet and re-approve. `request.md` distinguishes its immutable original request from appended dated user changes. `spec.md` remains concise; each requirement has stable `REQ-NNN`, acceptance, source and status. Decision records are conclusions/rationale rather than reasoning traces.

`plan.yaml` is only the compact dependency graph index; the individual `tasks/T-NNN.yaml` packets are canonical. On load, DADO rejects disagreement between packet IDs/dependencies and the graph. The graph is checked for missing IDs, cycles, duplicate IDs, impossible dependency/status combinations and invalid task schemas. A task packet includes goal, requirements, dependencies, allowed/forbidden file scopes, context references, acceptance, verification, risk and attempts. A result is compact and separate from verifier evidence. `verification/T-NNN.yaml` captures independent verdict/checks/time.

Structured work, index, config, plan, task, worker-result, and verifier-result artifacts have JSON Schema under package `schemas/` and copied project `.dado/schemas/`. YAML is safe-loaded; invalid state stops operations rather than being silently repaired. Evidence logs are selectively loaded and ignored from Git by default. Archive summaries are intentionally enough for cold-storage discovery without opening complete artifacts.
