# ADR-0004: Ownership hash manifest for updates

Status: accepted

Record hashes for DADO-managed OpenCode files. Preserve pre-existing same-path files and modified managed files during install/update; report conflicts. Remove only unchanged managed files on uninstall. This is simpler and safer than attempting to merge Markdown/YAML prompts. Trade-off: model configuration edits count as file modifications and can require manual merge or explicit force on a future update.
