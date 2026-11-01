# ADR-0004: Ownership hash manifest for updates

Status: accepted

Record hashes for DADO-managed OpenCode files. Preserve pre-existing same-path files and modified managed files during install/update; report conflicts. Remove only unchanged managed files on uninstall. This is simpler and safer than attempting to merge Markdown/YAML prompts. Model profiles are persisted separately in `.dado/config.yaml`; after installation/update, the active profile's model fields are projected back onto managed agents. Trade-off: `--force` can replace customized agent prompt content, but it does not discard profile data or active model/variant assignments.
