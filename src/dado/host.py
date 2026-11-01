"""Per-invocation OpenCode V2 model overlay; never edits tracked agent files.

The inline server config is applied to a private server (--standalone), not
an already-running shared service. Only model fields are supplied by DADO.
Verified using OpenCode v2.2.2's location-scoped /api/agent endpoint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any
from urllib.parse import urlencode

from .errors import ValidationError


def environment(profile: dict[str, dict[str, str | None]], base: dict[str, str] | None = None) -> dict[str, str]:
    from .models import validate_profile
    profile = validate_profile(profile)
    env = dict(os.environ if base is None else base)
    try:
        config: Any = json.loads(env.get("OPENCODE_CONFIG_CONTENT", "{}"))
    except ValueError as exc:
        raise ValidationError("OPENCODE_CONFIG_CONTENT must contain valid JSON") from exc
    if not isinstance(config, dict) or not isinstance(config.get("agents", {}), dict):
        raise ValidationError("OpenCode inline config and agents must be objects")
    agents = config.setdefault("agents", {})
    for role, selection in profile.items():
        if selection["model"] is None:
            # No personal override: retain the common project/host default.
            continue
        prior = agents.setdefault(role, {})
        if not isinstance(prior, dict):
            raise ValidationError(f"Invalid OpenCode inline agent {role}")
        prior["model"] = selection["model"] + (f"#{selection['variant']}" if selection["variant"] else "")
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    return env


def active_profile(project: Path) -> dict:
    from .models import _load_config, _validate_config_profiles
    name, profiles = _validate_config_profiles(_load_config(project))
    return profiles[name]


def check(project: Path, executable: str = "opencode") -> dict:
    """Read effective agents without any model generation; report overrides only."""
    profile = active_profile(project)
    endpoint = "/api/agent?" + urlencode({"location[directory]": str(project.resolve())})
    try:
        result = subprocess.run([executable, "api", "get", endpoint, "--standalone"], cwd=project,
                                env=environment(profile), capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValidationError("OpenCode could not verify the private-server model overlay")
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise ValidationError("Cannot inspect effective OpenCode agents") from exc
    if not isinstance(payload, dict) or payload.get("location", {}).get("directory") != str(project.resolve()):
        raise ValidationError("OpenCode returned agents for a different project location")
    actual = {agent["id"]: agent.get("model") for agent in payload.get("data", []) if isinstance(agent, dict) and "id" in agent}
    verified = []
    for role, selected in profile.items():
        if selected["model"] is None:
            continue
        provider, model = selected["model"].split("/", 1)
        effective = actual.get(role) or {}
        if (effective.get("providerID"), effective.get("id"), effective.get("variant")) != (provider, model, selected["variant"]):
            raise ValidationError(f"OpenCode did not apply the personal model selection for {role}")
        verified.append(role)
    return {"verified_roles": verified, "model_calls": 0, "server": "private", "inherit": "common project/host defaults"}


def launch(project: Path, arguments: list[str], executable: str = "opencode") -> int:
    if any(arg == "--server" or arg.startswith("--server=") for arg in arguments):
        raise ValidationError("Personal model overlays require a private server, not --server")
    command = [executable, *arguments]
    if "--standalone" not in arguments:
        command.append("--standalone")
    return subprocess.call(command, cwd=project, env=environment(active_profile(project)))
