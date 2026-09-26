"""OpenCode model discovery and minimally invasive agent frontmatter editing."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import yaml

from .errors import DadoError, ValidationError
from .fs import atomic_write

ROLES = ("dado-master", "dado-explorer", "dado-worker", "dado-verifier", "dado-researcher", "dado-historian")


def catalog(executable: str = "opencode") -> list[str]:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            out = subprocess.check_output([executable, "models"], text=True, stderr=subprocess.PIPE, timeout=30)
            models = [line.strip() for line in out.splitlines() if re.fullmatch(r"[^/\s]+/[^\s]+", line.strip())]
            if models:
                return models
            last_error = DadoError("`opencode models` returned no parseable provider/model entries")
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(0.15 * (attempt + 1))
    if isinstance(last_error, DadoError):
        raise last_error
    raise DadoError(f"Could not query `opencode models` after three attempts: {last_error}") from last_error


def set_agent_model(path: Path, model: str | None, available: list[str], variant: str | None = None) -> None:
    if model is not None and model.split("#", 1)[0] not in {item.split("#", 1)[0] for item in available}:
        raise ValidationError(f"Model is not in current OpenCode catalog: {model}")
    if variant is not None and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", variant) or model is None or "#" in model):
        raise ValidationError("Variant requires a selected base model and a valid variant ID")
    if variant:
        model = f"{model}#{variant}"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValidationError(f"Missing YAML frontmatter in {path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValidationError(f"Malformed YAML frontmatter in {path}")
    front = text[4:end]
    lines = front.splitlines()
    indices = [i for i, line in enumerate(lines) if re.match(r"^model\s*:", line)]
    if len(indices) > 1:
        raise ValidationError(f"Multiple model fields in {path}")
    if model is None:
        if indices:
            lines.pop(indices[0])
    else:
        value = f"model: {model}"
        if indices:
            lines[indices[0]] = value
        else:
            lines.append(value)
    updated = "---\n" + "\n".join(lines) + text[end:]
    atomic_write(path, updated)


def agent_model(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---" not in text[4:]:
        raise ValidationError(f"Malformed YAML frontmatter in {path}")
    front = yaml.safe_load(text[4:text.find("\n---", 4)]) or {}
    return front.get("model")
