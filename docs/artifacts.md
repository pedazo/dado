# Artifact reference

```text
.dado/
  config.yaml                 # DADO policy plus named per-role model/variant profiles
  managed-files.json          # ownership hashes for non-destructive updates
  work/
    index.yaml                # active-work IDs/paths and focused ID; statuses live in work.yaml
    project/{architecture,conventions,constraints,decisions}.md
    events.jsonl              # append-only state audit
    active/<work-id>/
      work.yaml request.md tasks/T-NNN.yaml evidence/
    archive/<year>/<work-id>/  # same artifacts plus summary.md
```

`work.yaml` carries work state, requirements with `REQ-NNN` acceptance/provenance/status, concise decisions, Git metadata, and explicit approval with a fingerprint of the request and task contract (mutable status and outcomes excluded). Contract edits require reapproval. An optional `related_commits` list can be added to a completed archived work after a user-requested commit; it does not alter approval or completion. `request.md` preserves the original request and appended dated clarifications.

`config.yaml` stores `profiles` keyed by a validated profile name, with a `{model, variant}` entry for each DADO role, and `active_profile`. A null model means inherit OpenCode's session/default model; a null variant means no explicit variant (and is required when inheriting). Only the active profile is projected into `.opencode/agents/*` frontmatter. Existing projects without a profile registry migrate their current role frontmatter into `default`; fresh projects initialize `default` to inherit. Agent frontmatter is the effective OpenCode representation, while the config registry is the durable source for profile switching and template updates.

The task packets are the dependency graph: DADO checks missing IDs, cycles, duplicates, invalid status/dependency combinations and schemas. Each packet includes goal, requirements, scope, context references, acceptance, checks and risk. Its `result` (worker claim) and `verdict` (independent assessment, including checked paths) are distinct fields, recorded only by their respective CLI operations. Changed contracts must be reapproved; tasks whose criteria changed since their start must be invalidated and rerun. Cancelled work may be archived without claiming completion after running/reviewing tasks are reconciled. Existing work using the earlier split-file format remains readable and writable; results from previous legacy attempts are preserved under `evidence/attempts/` on retry.

Structured work, index, config, task, worker-result, and verifier-result artifacts are validated with packaged JSON Schemas. YAML is safe-loaded; invalid state stops operations rather than being silently repaired. Evidence is loaded only when needed. Whether DADO data is version-controlled is decided by the host project. Archive summaries support cold-storage discovery without loading full artifacts.
