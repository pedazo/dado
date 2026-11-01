# ADR 0006: Durable local file transactions

## Status

Accepted.

## Context

DADO persists a small set of related files in a project checkout, including shared/local configuration and recovery metadata. Independent atomic file replacement does not make a multi-file operation crash-consistent. A local journal provides a recoverable declaration and replay boundary without introducing a database service.

## Decision

Use a journal under `.dado/.txn/` for supported multi-file operations. A transaction declares bounded file operations and trusted roots, then progresses through `building`, `pending`, and `committed`. A `building` journal may be incomplete and is never automatically replayed; `pending` is replayable; `committed` is already applied. Recovery is explicit and report-first: `dado repair` inspects, and `dado repair --apply` replays pending operations. No rollback is promised. A crash can leave a partially applied operation that must be replayed or reconciled.

Journals constrain target paths to caller-trusted roots; recorded journal roots are not authorization. Locks serialize participating operations on a local filesystem only. This is not a distributed lock, distributed transaction, database, or guarantee against non-participating external writers.

Fresh layout-v2 initialization separates shared policies from local profiles and operational checkout state. Legacy layouts remain readable until explicit migration (`dado migrate` previews; `dado migrate --apply` applies with a backup). Local model-profile handling must not rewrite tracked agent files. Runtime verification distinguishes declared checks from captured command records and binds successful verification to a code snapshot. Captured evidence records execution/provenance assertions, not proof of acceptance truth or authenticated actor identity.

## Consequences

- Recovery is inspectable and can replay pending declarations, but it does not restore the pre-transaction state.
- Malformed, unknown, or partially declared journals require inspection rather than guessed repair.
- Migration and profile/config updates depend on the local transaction journal and preserve a backup of legacy configuration.
- Budget ledgers are opt-in admission estimates, never provider billing; isolation and reservations coordinate only one checkout.
- Synthetic evaluation fixtures are not measurements. Policy output is advisory and starts no model processes.

## Operational guidance

Run `dado repair` before selecting `--apply`; inspect the reported phase and action. Never discard or edit a journal blindly. Re-run verification after code changes or snapshot integration. Share handoff files and free-text review notes only after checking for confidential content.
