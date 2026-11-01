"""Validated configuration/bootstrap shared by the runtime and local layout."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .errors import ValidationError
from .fs import file_lock, read_yaml
from .layout import config_path_for_profiles, is_layout2, load_effective_config, write_effective_config
from .models import initialize_profiles
from .validation import diagnostics

DEFAULTS = {
    "approval": {"plan": True},
    "scheduler": {"max_parallel_workers": 3},
    "verification": {"default_risk": "medium"},
    "history": {"automatic_search": False},
    "archive": {"completed": True},
}


def validate(config: dict) -> None:
    diagnostics("config", config, Path(__file__).parent / "schemas" / "config.schema.json")
    if "profiles" in config and config.get("active_profile") not in config["profiles"]:
        raise ValidationError("Invalid config: active_profile must name a configured model profile")


def load(project: Path) -> dict:
    if not (project / ".dado" / "config.yaml").exists():
        raise ValidationError("DADO config is missing; run `dado init` to restore defaults")
    config = load_effective_config(project)
    validate(config)
    return config


def initialize(project: Path) -> dict:
    shared_path = project / ".dado" / "config.yaml"
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(project / ".dado" / ".model-profiles"):
        v2 = is_layout2(project)
        local_path = config_path_for_profiles(project)
        shared_exists, local_exists = shared_path.exists(), local_path.exists()
        if shared_exists:
            shared = read_yaml(shared_path)
            if not isinstance(shared, dict):
                raise ValidationError("DADO config must contain a mapping")
            if v2 and {"profiles", "active_profile"} & set(shared):
                raise ValidationError("Shared config contains personal profiles; inspect `dado repair` or migrate explicitly")
            config = load_effective_config(project) if v2 and local_exists else shared
        else:
            config = deepcopy(DEFAULTS)
            if v2:
                config["verification"]["require_integration"] = True
                if local_exists:
                    local = read_yaml(local_path)
                    if not isinstance(local, dict) or set(local) - {"profiles", "active_profile"}:
                        raise ValidationError("Local config may contain only model profiles")
                    config.update(local)
        validate(config)
        changed = initialize_profiles(config, project / ".opencode" / "agents")
        validate(config)
        if changed or not shared_exists or not local_exists:
            write_effective_config(project, config)
    return config
