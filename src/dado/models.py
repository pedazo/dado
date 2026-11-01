"""OpenCode model discovery and named DADO role-profile management."""
from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlencode

import yaml

from .errors import ConflictError, DadoError, ValidationError
from .fs import atomic_write, file_lock, read_yaml
from .layout import config_path_for_profiles, is_layout2

try:  # durable journal is mandatory for layout2 profile writes
    from .transactions import FileTransaction
except ImportError:  # pragma: no cover - surfaces as an explicit error at call time
    FileTransaction = None

ROLES = ("dado-master", "dado-explorer", "dado-worker", "dado-verifier", "dado-researcher", "dado-historian")
DEFAULT_PROFILE = "default"
PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
VARIANT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*\Z")
MODEL_NAME = re.compile(r"[^/\s#]+/[^\s#]+\Z")
PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*\Z")
KEEP_MODEL = object()
KEEP_VARIANT = object()


def parse_model_catalog(payload: Any, project: Path) -> dict[str, dict[str, tuple[str, ...]]]:
    """Validate OpenCode's location-scoped /api/model response.

    The returned providers and models contain enabled entries only. Variant IDs
    come directly from the API response; this layer does not infer capabilities.
    """
    project = project.expanduser().resolve()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise DadoError("OpenCode returned an invalid structured model catalog")
    location = payload.get("location")
    if not isinstance(location, dict) or location.get("directory") != str(project):
        raise DadoError("OpenCode model catalog belongs to a different project location")

    providers: dict[str, dict[str, tuple[str, ...]]] = {}
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("enabled"), bool):
            raise DadoError("OpenCode returned an invalid model catalog entry")
        if not item["enabled"]:
            continue
        provider, identifier = item.get("providerID"), item.get("id")
        if (not isinstance(provider, str) or not PROVIDER_ID.fullmatch(provider)
                or not isinstance(identifier, str) or not MODEL_ID.fullmatch(identifier)):
            raise DadoError("OpenCode returned a model without a valid provider/model ID")
        model = f"{provider}/{identifier}"
        if not MODEL_NAME.fullmatch(model):
            raise DadoError("OpenCode returned an invalid provider/model ID")
        variants = item.get("variants")
        if not isinstance(variants, list) or any(
            not isinstance(variant, dict)
            or not isinstance(variant.get("id"), str)
            or not VARIANT_NAME.fullmatch(variant["id"])
            for variant in variants
        ):
            raise DadoError(f"OpenCode returned invalid reasoning variants for {model}")
        variant_ids = tuple(variant["id"] for variant in variants)
        if len(set(variant_ids)) != len(variant_ids):
            raise DadoError(f"OpenCode returned duplicate reasoning variants for {model}")
        models = providers.setdefault(provider, {})
        if identifier in models:
            raise DadoError(f"OpenCode returned a duplicate model: {model}")
        models[identifier] = variant_ids

    if not providers:
        raise DadoError("No enabled OpenCode models are available for this project")
    return {provider: dict(sorted(models.items())) for provider, models in sorted(providers.items())}


def discover_model_catalog(project: Path, executable: str = "opencode", timeout: float = 30.0
                           ) -> dict[str, dict[str, tuple[str, ...]]]:
    """Fetch enabled models and their advertised variant IDs for one project."""
    project = project.expanduser().resolve()
    if not project.is_dir():
        raise DadoError(f"Model catalog project is not a directory: {project}")
    if os.sep in executable or (os.altsep and os.altsep in executable):
        executable = str(Path(executable).expanduser().absolute())
    endpoint = "/api/model?" + urlencode({"location[directory]": str(project)})

    with tempfile.TemporaryFile() as capture:
        try:
            process = subprocess.Popen(
                [executable, "api", "get", endpoint],
                cwd=project,
                stdin=subprocess.DEVNULL,
                stdout=capture,
                stderr=subprocess.PIPE,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            raise DadoError(f"Cannot run OpenCode model discovery: {exc}") from exc
        try:
            _, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise DadoError(f"OpenCode model discovery timed out after {timeout:g} seconds") from exc
        if process.returncode:
            raise DadoError(
                "OpenCode model discovery failed; check that OpenCode V2 is installed and its API is available"
            )
        capture.seek(0)
        raw = capture.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise DadoError("OpenCode model catalog exceeds 16 MiB")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DadoError("OpenCode returned invalid JSON for its model catalog") from exc
    return parse_model_catalog(payload, project)


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


def validate_profile_name(name: str) -> str:
    if not isinstance(name, str) or not PROFILE_NAME.fullmatch(name):
        raise ValidationError("Profile names must start with a letter or digit and contain only letters, digits, '_' or '-' (1-64 characters)")
    return name


def empty_profile() -> dict[str, dict[str, str | None]]:
    return {role: {"model": None, "variant": None} for role in ROLES}


def _split_model(value: str | None, path: Path) -> dict[str, str | None]:
    if value is None:
        return {"model": None, "variant": None}
    if not isinstance(value, str):
        raise ValidationError(f"Model field must be a string in {path}")
    model, marker, variant = value.partition("#")
    if not MODEL_NAME.fullmatch(model):
        raise ValidationError(f"Invalid model assignment in {path}: {value!r}")
    if marker and not VARIANT_NAME.fullmatch(variant):
        raise ValidationError(f"Invalid model variant in {path}: {value!r}")
    return {"model": model, "variant": variant if marker else None}


def initialize_profiles(config: dict[str, Any], agents: Path) -> bool:
    """Migrate frontmatter assignments exactly once when no profile registry exists."""
    if "profiles" in config or "active_profile" in config:
        return False
    default = empty_profile()
    for role in ROLES:
        path = agents / f"{role}.md"
        # Pre-existing same-name user files are preserved by the installer; a file
        # without agent frontmatter has no legacy model assignment to migrate.
        if path.is_file() and path.read_text(encoding="utf-8").startswith("---\n"):
            default[role] = _split_model(agent_model(path), path)
    config["profiles"] = {DEFAULT_PROFILE: default}
    config["active_profile"] = DEFAULT_PROFILE
    return True


def _read_frontmatter(path: Path) -> tuple[str, list[str], str, dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValidationError(f"Missing YAML frontmatter in {path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValidationError(f"Malformed YAML frontmatter in {path}")
    try:
        front = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"Malformed YAML frontmatter in {path}: {exc}") from exc
    if not isinstance(front, dict):
        raise ValidationError(f"YAML frontmatter must be a mapping in {path}")
    lines = text[4:end].splitlines()
    indices = [i for i, line in enumerate(lines) if re.match(r"^model\s*:", line)]
    if len(indices) > 1:
        raise ValidationError(f"Multiple model fields in {path}")
    value = front.get("model")
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"Model field must be a string in {path}")
    return text, lines, text[end:], front


def agent_model(path: Path) -> str | None:
    return _read_frontmatter(path)[3].get("model")


def render_agent_model(path: Path, model: str | None, variant: str | None = None,
                       available: list[str] | None = None) -> str:
    """Validate and render one model field while preserving other frontmatter and body."""
    if model is not None and not MODEL_NAME.fullmatch(model):
        raise ValidationError(f"Invalid base model: {model!r}")
    if variant is not None and (not VARIANT_NAME.fullmatch(variant) or model is None):
        raise ValidationError("Variant requires a selected base model and a valid variant ID")
    if model is not None and available is not None:
        if model not in {entry.split("#", 1)[0] for entry in available}:
            raise ValidationError(f"Model is not in current OpenCode catalog: {model}")
    _, lines, suffix, _ = _read_frontmatter(path)
    indices = [i for i, line in enumerate(lines) if re.match(r"^model\s*:", line)]
    if model is None:
        if indices:
            lines.pop(indices[0])
    else:
        value = f"model: {model + '#' + variant if variant else model}"
        if indices:
            lines[indices[0]] = value
        else:
            lines.append(value)
    return "---\n" + "\n".join(lines) + suffix


def set_agent_model(path: Path, model: str | None, available: list[str], variant: str | None = None) -> None:
    """Compatibility helper for editing one agent directly."""
    if model is not None and "#" in model:
        model, _, embedded_variant = model.partition("#")
        if variant is not None:
            raise ValidationError("Specify a variant either in the model ID or separately, not both")
        variant = embedded_variant or None
    updated = render_agent_model(path, model, variant, available)
    if path.read_text(encoding="utf-8") != updated:
        atomic_write(path, updated)


def _validate_role_model(role: str, value: Any, available: list[str] | None = None) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != {"model", "variant"}:
        raise ValidationError(f"Invalid model profile entry for {role}")
    model, variant = value["model"], value["variant"]
    if model is not None and (not isinstance(model, str) or not MODEL_NAME.fullmatch(model)):
        raise ValidationError(f"Invalid base model for {role}")
    if variant is not None and (not isinstance(variant, str) or not VARIANT_NAME.fullmatch(variant) or model is None):
        raise ValidationError(f"Invalid Variant for {role}; variants require a selected base model")
    if available is not None and model is not None and model not in {entry.split("#", 1)[0] for entry in available}:
        raise ValidationError(f"Model is not in current OpenCode catalog: {model}")
    return {"model": model, "variant": variant}


def validate_profile(profile: Any, available: list[str] | None = None) -> dict[str, dict[str, str | None]]:
    if not isinstance(profile, dict) or set(profile) != set(ROLES):
        raise ValidationError("A model profile must define every DADO role")
    return {role: _validate_role_model(role, profile[role], available) for role in ROLES}


def _agent_paths(project: Path) -> dict[str, Path]:
    return {role: project / ".opencode" / "agents" / f"{role}.md" for role in ROLES}


def _require_agents(project: Path) -> dict[str, Path]:
    paths = _agent_paths(project)
    missing = [role for role, path in paths.items() if not path.is_file()]
    if missing:
        raise ValidationError(f"DADO agents are missing: {', '.join(missing)}; run `dado init`")
    return paths


def _write_changes(changes: dict[Path, str]) -> None:
    """Atomically replace each file, restoring prior contents if a write fails."""
    originals = {path: path.read_text(encoding="utf-8") for path in changes}
    written: list[Path] = []
    try:
        for path, content in changes.items():
            if originals[path] != content:
                atomic_write(path, content)
                written.append(path)
    except OSError:
        for path in reversed(written):
            try:
                atomic_write(path, originals[path])
            except OSError:
                pass
        raise


def _apply_changes(project: Path, changes: dict[Path, str],
                   *, roots: Sequence[Path] | None = None,
                   label: str = "dado-models") -> None:
    """Apply layout2 local profile writes through the durable journal.

    The transactions module is a hard requirement — there is no silent
    atomic fallback when it is unavailable. ``roots`` declares the trusted
    write containment: default ``[.dado]`` for profile-state-only writes;
    any caller writing agent files MUST pass ``[.dado, .opencode]``
    (layout2 doesn't: projections go through the host wrapper).
    """
    if FileTransaction is None:
        raise ConflictError(
            "Durable transaction journal unavailable; refusing unsafe profile writes "
            "(no silent atomic fallback)"
        )
    journal_roots = [project / ".dado"] if roots is None else [Path(r) for r in roots]
    tx = FileTransaction.begin(project / ".dado", roots=journal_roots, label=label)
    for path, content in changes.items():
        tx.write(path, content)
    tx.apply()


def _load_config(project: Path) -> dict[str, Any]:
    value = read_yaml(config_path_for_profiles(project))
    if not isinstance(value, dict):
        if is_layout2(project):
            raise ValidationError("DADO model profiles are missing in config.local.yaml; run `dado init`")
        raise ValidationError("DADO config must contain a mapping; run `dado init`")
    return value


def seed_local_profiles(project: Path) -> bool:
    """Layout2 bootstrap for freshly cloned or initialized projects.

    Creates the ignored local profile registry once (``config.local.yaml``),
    seeding role entries from existing agent frontmatter exactly like legacy
    ``Store.init`` did; absent agents yield a fully inherit (``None``) default
    profile. Legacy projects are left untouched (returns False). The CLI init
    wiring calls this after layout initialization and ``Store.init``.
    """
    if not is_layout2(project):
        return False
    path = config_path_for_profiles(project)
    existing = read_yaml(path)
    if isinstance(existing, dict) and ("profiles" in existing or "active_profile" in existing):
        return False  # already seeded or user-managed; never overwrite
    if isinstance(existing, dict) and not existing:
        existing = None  # an empty file is not user state either
    base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    initialize_profiles(base, project / ".opencode" / "agents")
    _apply_changes(project, {path: _save_config(base)})
    return True


def _save_config(config: dict[str, Any]) -> str:
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100)


def _validate_config_profiles(config: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    profiles = config.get("profiles")
    active = config.get("active_profile")
    if not isinstance(profiles, dict) or not profiles or DEFAULT_PROFILE not in profiles:
        raise ValidationError("DADO model profiles are not initialized; run `dado init`")
    for name, profile in profiles.items():
        validate_profile_name(name)
        validate_profile(profile)
    validate_profile_name(active)
    if active not in profiles:
        raise ValidationError(f"Active model profile does not exist: {active}")
    return active, profiles


def edit_profile(project: Path, name: str, updates: dict[str, dict[str, Any]],
                 available: list[str]) -> None:
    """Create/edit a profile; in layout2 nothing is projected to agent files."""
    validate_profile_name(name)
    unknown = [role for role in updates if role not in ROLES]
    if unknown:
        raise ValidationError(f"Unknown DADO agent: {unknown[0]}")
    with file_lock(project / ".dado" / ".model-profiles"):
        config = _load_config(project)
        active, profiles = _validate_config_profiles(config)
        if name in profiles:
            profile = copy.deepcopy(profiles[name])
        else:
            profile = copy.deepcopy(profiles[DEFAULT_PROFILE])
        for role, spec in updates.items():
            if not isinstance(spec, dict) or set(spec) != {"model", "variant"}:
                raise ValidationError(f"Invalid model selection for {role}")
            old = profile[role]
            model = old["model"] if spec["model"] is KEEP_MODEL else spec["model"]
            if spec["variant"] is KEEP_VARIANT:
                variant = old["variant"] if model == old["model"] else None
            else:
                variant = spec["variant"]
            profile[role] = {"model": model, "variant": variant}
        profile = validate_profile(profile)
        for role in updates:
            profile[role] = _validate_role_model(role, profile[role], available)
        profiles[name] = profile
        config_changes = {config_path_for_profiles(project): _save_config(config)}
        if is_layout2(project):
            # Layout2 writes registry state only; model projection happens via
            # the host wrapper (`dado host exec`), so agent files are never
            # modified — tracked or not. Model None means "inherit".
            _apply_changes(project, config_changes)
            return
        # Legacy (no layout marker): preserve in-place frontmatter projection.
        paths = _require_agents(project)
        changes: dict[Path, str] = {}
        if active == name:
            for role, path in paths.items():
                if role not in updates:
                    continue
                spec = profile[role]
                changes[path] = render_agent_model(path, spec["model"], spec["variant"], available)
        _write_changes({**config_changes, **changes})


def use_profile(project: Path, name: str, available: list[str]) -> None:
    validate_profile_name(name)
    with file_lock(project / ".dado" / ".model-profiles"):
        config = _load_config(project)
        _, profiles = _validate_config_profiles(config)
        if name not in profiles:
            raise ValidationError(f"Unknown model profile: {name}")
        profile = validate_profile(profiles[name], available)
        config["active_profile"] = name
        config_changes = {config_path_for_profiles(project): _save_config(config)}
        if is_layout2(project):
            # Layout2: activation is local binding state only. Model entries
            # reach OpenCode through the host wrapper (OPENCODE_CONFIG_CONTENT
            # via `dado host exec`); agent files are never modified, so
            # personal settings never dirty tracked agent files. Entries with
            # model None keep the shared project default (they do not clear it).
            _apply_changes(project, config_changes)
            return
        # Legacy (no layout marker): preserve in-place frontmatter projection.
        paths = _require_agents(project)
        changes: dict[Path, str] = {}
        for role, path in paths.items():
            spec = profile[role]
            changes[path] = render_agent_model(path, spec["model"], spec["variant"], available)
        changes.update(config_changes)
        _write_changes(changes)


def list_profiles(project: Path) -> tuple[str, list[str]]:
    config = _load_config(project)
    active, profiles = _validate_config_profiles(config)
    return active, sorted(profiles)


def active_profile_spec(project: Path) -> dict[str, dict[str, str | None]]:
    """Model entries of the active profile, ready for the host wrapper.

    Layout2 projects never rewrite `model:` in agent files; the spawn path
    (CLI/store wiring) passes these entries to ``dado.host.environment`` so
    OpenCode receives ``model: provider/model#variant`` in
    OPENCODE_CONFIG_CONTENT instead. A ``None`` model entry means "no personal
    override" and keeps the shared project default.
    """
    config = _load_config(project)
    active, profiles = _validate_config_profiles(config)
    return validate_profile(profiles[active])


def sync_managed_active_profile(project: Path) -> None:
    """Reapply active model assignments after install/update, only to managed agents."""
    manifest_path = project / ".dado" / "managed-files.json"
    config_path = project / ".dado" / "config.yaml"
    if not manifest_path.is_file() or not config_path.is_file():
        return
    with file_lock(project / ".dado" / ".model-profiles"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"Cannot read DADO managed-files manifest: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValidationError("Invalid DADO managed-files manifest")
        if is_layout2(project):
            # Layout2: agent projection is delegated to the host wrapper at
            # spawn time (`dado host exec`), so nothing here writes agent
            # files — this keeps tracked agents clean after install/update.
            return
        config = _load_config(project)
        active, profiles = _validate_config_profiles(config)
        profile = validate_profile(profiles[active])
        roles = [
            role for role, path in _agent_paths(project).items()
            if manifest.get(f"agents/{role}.md") and path.is_file()
        ]
        changes = {
            project / ".opencode" / "agents" / f"{role}.md": render_agent_model(
                project / ".opencode" / "agents" / f"{role}.md",
                profile[role]["model"], profile[role]["variant"],
            )
            for role in roles
        }
        # Legacy behavior preserved exactly: in-place atomic writes with the
        # legacy rollback writer (not the layout2 journal).
        _write_changes(changes)
