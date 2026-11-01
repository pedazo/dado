# Evolution verification report

## Scope and measurement limits

Implementation was performed on the existing dirty checkout, preserving earlier changes. No commits or pushes were made. Paid model-evaluation budget was zero: no real-model comparison was run, and the synthetic evaluation fixtures establish no quality or cost advantage.

Local checks use Linux and Python 3.13. CI is configured for Ubuntu/Windows and Python 3.10/3.13; remote matrix execution has not been observed. POSIX-only test expectations are explicitly gated; symlink tests check host capability.

## Runtime comparison

Command: `python scripts/benchmark_runtime.py --sizes 10 100 500 --output <file>`.
The saved source baseline includes the user's pre-existing Store changes, before this integration. Measurements below are one local run per size, not statistical estimates. Timings depend on filesystem and hardware. Fsync counts include the new journal and directory durability work.

For 500 tasks:

| Operation | Before (s) | After (s) | Fsync before → after |
| --- | ---: | ---: | ---: |
| Load | 0.247088 | 0.248964 | 0 → 0 |
| Ready | 0.255408 | 0.251403 | 0 → 0 |
| Unchanged save | 0.199145 | 0.173718 | 500 → 0 |
| One-task save | 0.194918 | 0.183091 | 500 → 11 |
| Approve | 0.772527 | 0.249724 | 2 → 3 |
| Start | 0.703799 | 0.429896 | 502 → 13 |
| Result | 0.717074 | 0.427189 | 501 → 12 |
| Invalidate | 0.462218 | 0.422639 | 501 → 12 |
| Archive cancelled | 0.259649 | 0.530035 | 4 → 1035 |
| Invalidate dependency chain | 0.050066 | 0.000291 | 0 → 0 |

Unchanged packets avoid writes; a one-task change no longer synchronizes all 500 packets. Archive is slower and performs substantially more synchronization because every moved file is journalled and directory changes are made durable. This is an explicit recovery tradeoff, not a claim of universal speedup. Raw local runs are `/tmp/opencode/dado-runtime-before.json` and `/tmp/opencode/dado-runtime-after.json` in the implementation environment.

## Operational checks

- Full local suite: **328 passed**, using `.venv/bin/python -m pytest -q`. Compile checks and `git diff --check` passed.
- Wheel/sdist build and installed-wheel CLI/resource smoke passed; the wheel contains all 16 schema/template resources. Reproduce with `python -m build` and `python scripts/check_package.py`.
- Fresh layout-v2 CLI initialization, local profile listing, private-server host configuration check and repair preview passed.
- Disposable CLI lifecycle passed: captured task check → independent-verdict fixture → captured integration check → completed → archived. No model generation was involved.
- Independent reviews exercised interrupted preparation, trusted replay roots, process/thread locks, stale/tampered evidence, migration conflicts, imported approval reset, isolation conflicts and preservation of intervening user edits.
- OpenCode v2.2.2 configuration-only probes confirmed per-agent model/variant overlays via a private standalone server. A changed local profile requires a new wrapper invocation.

## Remaining external validation

Real agent comparisons and policy calibration require a separately approved sample and budget. Adaptive routing remains optional. The configured remote CI matrix must run before claiming Windows/Python-version coverage. Local evidence hashes detect changes but do not authenticate an independent model identity. Budget reservations are admission accounting, not provider billing; isolation reservations are checkout-local, not distributed coordination.
