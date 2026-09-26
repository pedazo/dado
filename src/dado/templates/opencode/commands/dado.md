---
description: Start or continue durable DADO work
agent: dado-master
---

Use DADO for this request: $ARGUMENTS

First inspect focused durable state using `dado status`. For a new trivial edit without an explicit route, ask whether to do it directly or track it with DADO; `/dado` alone does not choose. A tracked trivial edit uses a compact one-task plan with normal approval and verification. An explicitly requested one-off commit is delegated directly to a worker and linked to an archived work only after a confirmed Git commit and confident attribution. For other new durable work, preserve this exact request in its original language. Follow the `dado-protocol` skill. Cancel and archive mistaken work. Keep the response concise and never claim deterministic state changes unless the CLI confirms them.
