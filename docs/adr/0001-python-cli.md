# ADR-0001: Python CLI/runtime

Status: accepted

Use Python 3.10+ for the local cross-platform CLI and deterministic runtime. It supports Windows/macOS/Linux, straightforward pipx/venv installs, broad test availability, and keeps YAML/JSON Schema filesystem operations small. Runtime dependencies are limited to PyYAML and jsonschema; no external services or LLM library is required. Trade-off: Python is a user prerequisite and packages do not create a standalone binary.
