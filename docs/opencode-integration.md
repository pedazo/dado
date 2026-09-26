# OpenCode integration and installation

The adapter follows official OpenCode V2 documentation checked against installed `opencode v2.0.16` and the current official docs (agents, permissions, models, commands, skills, CLI). Project agents use `.opencode/agents/<name>.md`; primary/subagent modes are native; V2 permissions are `permissions` arrays of action/resource/effect rules. `/dado` is a native Markdown command and the protocol is a progressive-disclosure skill. Model assignments live only in agent frontmatter.

`dado models` calls the actual `opencode models` CLI catalog each time; it does not keep a static list. It supports current catalog IDs and `inherit` (removing the agent's model field). Bulk selection is available, e.g. `dado models --model provider/model --variant high --roles dado-worker dado-explorer`. Interactive mode selects a model or inherit and an optional variant separately for each role. `--variant none` clears a variant while keeping the model. Variant IDs are model-specific; `opencode models` does not list supported variants, so OpenCode checks availability when the agent runs. OpenCode's default model remains managed by OpenCode itself.

## Safe init and update

`dado init` creates missing `.dado` directories, config and project memory and installs only the DADO agent, command and skill files under existing `.opencode/`. Pre-existing same-name files are treated as user-owned and preserved. DADO-installed templates get hashes in `.dado/managed-files.json`; on update, unchanged managed files may be refreshed, while locally modified files are preserved and reported. `--force` opts in to replacing modified managed templates. Templates with model customizations will count as modified; compare/merge intentionally before `dado update --force`. If the manifest is missing, uninstall refuses to guess which files belong to DADO and asks for manual inspection.

Global executable installation is separate from project initialization. From source, `python -m pip install .` (or use `scripts/install.sh` / `scripts/install.ps1`). Update from source with `python -m pip install --upgrade .`, then run `dado update` inside the project. Remove the CLI with `pipx uninstall dado-orchestration` or `python -m pip uninstall dado-orchestration`. `dado uninstall` removes only unchanged managed OpenCode files. It preserves `.dado/work` by default; `--remove-data` requires typing `DELETE WORK`.

## Permissions and limitation

The role files use V2 action/resource/effect rules. Provider/model permission details can vary by OpenCode release. Review the installed frontmatter and effective project/global config with OpenCode's own tools. Agent permission rules do not enforce file scope at the operating-system level. The worker must report changed files; DADO rejects result packets that claim changes outside the declared scope. No plugin tools are installed: DADO's CLI is the deterministic tool boundary, invoked through native OpenCode shell access, avoiding an extra TypeScript plugin runtime that would duplicate state logic and currently has a different V2 plugin API.

Sources: https://opencode.ai/v2/docs/agents/ ; https://opencode.ai/v2/docs/permissions/ ; https://opencode.ai/v2/docs/commands/ ; https://opencode.ai/v2/docs/skills/ ; https://opencode.ai/v2/docs/models/ ; https://opencode.ai/v2/docs/cli/ .
