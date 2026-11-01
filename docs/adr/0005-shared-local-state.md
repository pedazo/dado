# ADR 0005: Shared vs. local DADO state (multi-user checkout layout)

- Status: Approved (phase 0)
- Date: 2026-11-01
- Scope: layout identity/versioning, shared/local config split, migration of
  legacy single-config projects, tracked-file protection, installer hardening.
  Store/CLI wiring is a follow-up integration task; existing Store/CLI code and
  schema are intentionally untouched in phase 0.

## Context

Today everything about a project's DADO configuration lives in one physical
file, `.dado/config.yaml`, and model assignments are projected directly into
`.opencode/agents/<role>.md`. In a repository cloned by several users this
mixes state that should be shared (policies, durable decisions) with state that
belongs to one checkout/user (active model profile, per-clone identity,
locked/cached work). Two concrete problems:

1. Editing a model profile dirties tracked files (`.dado/config.yaml`, tracked
   agents) in every user's working tree, so unrelated work turns to conflicts.
2. Identity is implicit: work folders and events are keyed only by on-disk
   paths/slugs, so two checkouts of the same project are indistinguishable.

Meanwhile `.dado` is ignored wholesale in this repository, which hides even the
*shared* artifacts (policies, project knowledge) that clones should receive.

## Decision

### 1. Versioned layout marker

`layout.migrate` and `models._apply_changes` take the trusted journal as a
hard requirement; the journal roots declared by each call site are:

- profile-state-only writes (layout2 `edit_profile`/`use_profile`,
  `seed_local_profiles`, `write_effective_config`, migration):
  `roots=[.dado]`;
- any code path that journals agent-file writes must declare
  `[.dado, .opencode]` — legacy paths write agents through the legacy
  rollback writer instead, preserving historical behavior byte-for-byte.

### 1. Versioned layout marker

`.dado/layout.yaml` (committed) is the single versioned layout marker:

```yaml
version: 2
project_uuid: <uuid4, stable across clones>
created_at: <UTC timestamp>
```

Presence of a layout marker with `version: 2` ("layout2") selects the new
behavior. **No marker ⇒ legacy behavior is preserved exactly**: profiles keep
being read from and edited in `.dado/config.yaml`; legacy projects remain
fully readable and editable until an explicit migration.

### 2. Identities (no path-only identity)

- `project_uuid` lives in the shared, versioned `layout.yaml` — identical
  across clones, identifies the project, not the checkout.
- `checkout_uuid` lives in the ignored local identity file `.dado/identity`
  (per clone, stable for the life of that clone).
- Work and executions use freshly minted UUIDs (`work_uuid`,
  `execution_uuid`); directory slugs remain human-friendly but are no longer
  identity. Events may reference dicts alongside paths so two clones can never
  collide or be confused.

### 3. Shared and local config split (layout2 only)

- `.dado/config.yaml` — committed, **policies only**: `approval`,
  `scheduler`, `verification`, `history`, `archive`. Unknown keys and profile
  keys are rejected here.
- `.dado/config.local.yaml` — ignored, **profile state only**:
  `profiles` and `active_profile` (schema-paired). Policy keys are rejected
  here: a checkout can never relax a shared policy locally. Unknown keys are
  rejected in both files.
- `.dado/work/` remains the physical artifact tree (`active/`, `archive/`,
  `imports/`, `index.yaml`, `events.jsonl`), but each of those entries is
  ignored individually by `.dado/.gitignore` — together with the layout
  runtime state (`local/` for the isolation runtime, `backups/`, `.txn/`,
  `managed-files.json`, identity, `*.lock`). Only explicit promotion (e.g.
  `work/project/*`) makes shared work content trackable. The tracked-file
  inventory classifies every tracked `.dado` path into `tracked_shared_work`
  (only `work/project/**`) vs `tracked_operational` (everything else), so
  tracked runtime residue is always visible.

### 4. Generated `.dado/.gitignore`

`dado layout init` (new CLI init will invoke layout initialization before
`Store.init`) generates/extends `.dado/.gitignore` *safely*: existing entries
and user lines are never removed or reordered, missing entries are appended
once, and tracked files are never deleted or rewritten to make them
ignorable. Required entries (local config, identity, managed manifest,
`local/` isolation runtime, backups, transaction journals, lock files, and
the transient work tree including `work/imports/`) make the split
self-maintaining across clones.

### 5. Model projection goes through the host wrapper (agents never edited)

Layout2 DADO tooling never writes `model:` into `.opencode/agents/<role>.md`
— tracked or untracked. Personal model settings live in
`config.local.yaml`; at spawn time entries are injected by the verified host
mechanism: ``dado.host.environment(profile, base)`` builds
`OPENCODE_CONFIG_CONTENT` with `agents: {<role>: {model: "provider/model#variant"}}`,
``host.launch`` runs OpenCode `--standalone` and ``dado host check`` verifies
via `/api/agent?location[directory]` that OpenCode received the expected
model+variant (no model calls, no paid APIs).

- Entries whose model is `None` mean **inherit**: no personal override is
  injected and the shared project default (tracked template) keeps deciding.
  `None` never clears a tracked model.
- After `install`/`update` there is nothing to rewrite, so tracked agent
  files never turn dirty from model work; `dado dado host exec -- …`
  documents the spawn path in the CLI UX.
- Legacy projects (no layout marker) keep the previous behavior: profile
  state in `.dado/config.yaml` with in-place frontmatter projection into
  agent files.

### 6. Migration (`dado migrate` / `layout.migrate`)

`migrate(project, dry_run=True | apply=True)`:

1. **Inventory** — read the legacy `.dado/config.yaml`, take its SHA-256, and
   record which operational files git tracks (`git ls-files` for `.dado/` and
   `.opencode/`, including tracked role agents, as a diagnostic).
2. **Back up locally** — copy the original config bytes to
   `.dado/backups/layout-migration/<timestamp>-<hash>/{config.yaml,manifest.json}`.
   Backups are local and ignored; nothing is removed.
3. **Extract and mark atomically** — one `FileTransaction` (journalled under
   `.dado/.txn/`) writes the policies-only shared config, the local profile
   config, and the `version: 2` layout marker together. A crash leaves the
   journal pending and replayable; the marker only flips when the extraction
   landed.
4. **Idempotent** — re-running on a migrated project is a no-op
   (`status: already`); a dry run writes nothing and only reports.
5. **Guards that never become overwrites** — git history, remotes and
   tracked files are never touched (tracked residue is reported; the
   shared-side change is committed by the user). An existing divergent
   `config.local.yaml` is *rejected untouched*: user preferences are never
   silently overwritten by the legacy extraction. Applied migrations are
   serialized on the well-known `.model-profiles` lock — callers that own
   that lock must not re-acquire it (`write_effective_config` never locks
   itself; nesting the same lock path would deadlock).
6. **Strict marker gate** — a `layout.yaml` that exists but is malformed,
   has an unknown version, or lacks a project UUID is an explicit
   `ConflictError`; layout state is never silently guessed as legacy.

### 7. Repair (`layout.repair_layout(project, apply=False)`)

Report-first recovery of `.dado/.txn` journals with the trusted root set
`[.dado, .opencode]` — a journal's own recorded roots are never authority:

- `building` journals are only for inspection (never replayed; partial
  declarations cannot be completed by replay) — discard or seal by the
  creator;
- `pending` journals are replayed only with `apply=True`; apply is
  idempotent and refuses foreign edits (`ConflictError`), per-journal
  refusals are reported instead of aborting the whole repair;
- unreadable journals are reported for manual inspection;
- the transactions module is a hard requirement everywhere (migration,
  `write_effective_config`, profile writes, repair): no silent atomic
  fallback when the journal is unavailable.

### 8. Installer hardening

`installer.install()` keeps its non-destructive manifest-preserving behavior
(manifest stays local: `.dado/managed-files.json`, POSIX-relative keys) and
adds:

- refuse to write through a symlink target (no overwrite escape) and reject
  install when `.opencode` itself resolves outside the project;
- per-target containment check of the resolved target inside `.opencode`;
- uninstall likewise refuses to remove through symlinks.

### 9. Compatibility helpers (integration seams)

Phase 2 Store/CLI wiring consumes only these:

- `layout.config_path_for_profiles(project)` — local profile config path in
  layout2, legacy `config.yaml` otherwise.
- `layout.load_effective_config(project)` — merged view: shared policies +
  local profile state. Legacy projects get their full legacy config back.
- `layout.write_effective_config(project, config[, bootstrap])` — splits and
  writes: policy keys into shared config, profile keys into the profiles
  config (legacy mode keeps writing everything to `config.yaml`). The
  journal is mandatory; serialization is caller-owned (`.model-profiles`
  lock; never nested). `bootstrap=True` tolerates a missing shared config on
  the explicit init/seed path only.
- `layout.repair_layout(project, *, apply=False)` — inspect/replay `.txn`
  journals with trusted roots `[.dado, .opencode]` (see section 7). All
  layout restore flows: `models.seed_local_profiles(project)` seeds the
  local profile registry per clone (frontmatter-twice-once semantics,
  idempotent) right after layout initialization and `Store.init`.

## Consequences

- Zero behavior change unless a project is migrated or initialized fresh:
  all legacy tests (which edit/read profiles in `.dado/config.yaml`) pass
  untouched.
- Users stop seeing model-profile churn in `git status`; policies and project
  knowledge become the shared, committable part of DADO state, and personal
  model settings ride the verified host wrapper instead of tracked files.
- Agent templates stay the single source of the shared project default; a
  `None` model entry inherits it rather than clearing it. `dado host check`
  documents what OpenCode actually receives at spawn time.
- The tracked-file inventory stays in the migration report for visibility:
  the shared-side config/layout change should be committed by the user, who
  remains the owner of tracked content.

## Alternatives considered

- *Per-user config profiles committed with merge markers* — rejected: profile
  churn in shared history is the problem being solved, and gating one set of
  users' state on git merge behavior is fragile.
- *Frontmatter projection into agents only when untracked* — superseded by
  the verified host wrapper: even untracked agents stay template-pure in
  layout2, so one path handles clones with and without tracked agents.
- *Auto-rewriting tracked `.dado/.gitignore` / removing tracked files to make
  the split clean* — rejected: `.dado` content is user-owned; the layout code
  only appends safe ignore entries and reports tracked conflicts.
