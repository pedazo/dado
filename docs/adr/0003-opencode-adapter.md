# ADR-0003: Native OpenCode agents, command and skill

Status: accepted

Use official OpenCode V2 Markdown agents, permissions, command, skill, and per-agent model frontmatter. DADO CLI performs deterministic state operations. Do not install a custom-tools plugin in this version: the V2 plugin API is version-sensitive and the CLI already provides a coherent validated tool boundary without a second implementation path. Trade-off: the agent invokes CLI commands via permitted shell and must honor documented handoffs; runtime does not itself dispatch LLM sessions.
