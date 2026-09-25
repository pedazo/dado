# ADR-0002: Human-readable artifacts as primary state

Status: accepted

Persist work as YAML/Markdown and use JSON Schema, atomic temp-and-replace for individual files, a local lock directory, and append-only JSONL events. Avoid a database: artifacts are diffable, Git-friendly, inspectable after conversation loss, and sufficient for a single-user local workflow. Trade-off: archive/index operations across files are not ACID transactions; recovery/doctor must identify rather than conceal partial operations.
