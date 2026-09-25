# Recovery and interrupted-run acceptance

Conversation history is optional. Start in the same project and run `dado status`, `dado doctor`, or `dado resume <work-id>`. The focused ID and active artifacts are enough to reconstruct state. Never rebuild from memory if files disagree; validate and escalate corruption.

| Crash point | Recovery |
|---|---|
| Before worker start | Task remains pending/ready; scheduler can offer it. |
| While `running`, no result | Outcome unknown; return to ready when dependencies are met. A worker may have made partial source edits; inspect Git diff before retry. |
| Result recorded as `review` | Keep task in review and rerun only the independent verifier unless durable PASS was already recorded. |
| Verification PASS recorded | Task remains done; don't repeat. |
| During archive | Artifact move and index update are not a multi-file transaction. `doctor`/manual inspection required if interruption splits these operations; no archive is silently reconstructed. |

Atomic file replace limits corruption of individual artifacts. A process-wide lock serializes local mutations, but multi-artifact work creation/archive is not a transactional database; event log and filesystem/index can be compared. Never automatically delete or rewrite user artifacts to repair state.

## Automated acceptance

`tests/test_runtime.py::test_interrupted_work_recovery_full_acceptance_and_archive` simulates feature request capture, two DAG tasks, completed+independently verified T-001, T-002 in-flight process loss, new Store instance recovery, continuing only T-002, requirement completion, summary, and archive. It verifies that T-001 is not rerun. Other tests cover isolation, conflicts, stale propagation, DAG validation, independent verifier state, and model assignment. The scenario uses deterministic in-process agents represented by durable packets; live LLM invocation is intentionally governed by OpenCode and requires user provider setup.
