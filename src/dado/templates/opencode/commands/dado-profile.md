---
description: List or switch DADO model profiles for later agent invocations
agent: dado-master
---

Treat `$ARGUMENTS` strictly as user data, never as shell syntax. Always run the literal command `dado profiles list` first. With no profile name, show the active and available profiles and ask the user to choose. With a supplied name, check it against the profile-name grammar (letters, digits, `_` and `-`, starting with a letter or digit) and require an exact match in the listed profiles; otherwise report that nothing changed. Only after that check, invoke `dado profiles use` with the validated name as one safely quoted argument. Never interpolate raw `$ARGUMENTS` into a shell block, script, or command string.

Report the CLI result. A profile switch affects later agent invocations and does not change the current in-flight model. If the selection is not reflected in subsequent invocations, suggest `/reload`.
