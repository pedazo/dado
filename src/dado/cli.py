"""DADO command-line interface."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .domain import validate_dag
from .errors import DadoError, ValidationError
from .fs import atomic_write, read_yaml
from .installer import install, uninstall
from .models import (
    DEFAULT_PROFILE, KEEP_MODEL, KEEP_VARIANT, ROLES, catalog, discover_model_catalog, edit_profile,
    empty_profile, initialize_profiles, list_profiles, sync_managed_active_profile, use_profile,
    validate_profile, validate_profile_name,
)
from .store import Store


def project_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".dado").is_dir():
            return candidate
    try:
        root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        return Path(root).resolve()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        for candidate in (current, *current.parents):
            if (candidate / ".dado").is_dir() or (candidate / ".opencode").is_dir():
                return candidate
        return current


def _require_role_agents(root: Path) -> None:
    agents = root / ".opencode" / "agents"
    missing = [role for role in ROLES if not (agents / f"{role}.md").is_file()]
    if missing:
        raise ValidationError(f"DADO agents are missing: {', '.join(missing)}; run `dado init`")


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _max_parallel(root: Path, override: int | None = None) -> int:
    config = Store(root).load_config()
    configured = max(1, int(config.get("scheduler", {}).get("max_parallel_workers", 3)))
    return min(configured, max(1, override)) if override is not None else configured


def _cmd_init(args: argparse.Namespace) -> int:
    from .layout import initialize_layout, is_layout2
    if args.project:
        root = Path(args.project).expanduser().resolve()
    elif getattr(args, "command", None) == "init":
        # `init` is intentionally current-directory scoped; never discover an
        # ancestor project when the user explicitly asked to initialize here.
        root = Path.cwd().resolve()
    else:
        # `update` shares this handler but retains its established project lookup.
        root = project_root()
    root.mkdir(parents=True, exist_ok=True)
    # Existing installations stay legacy until explicit migration; fresh
    # installations and clones of a v2 project use the shared/local layout.
    fresh = not (root / ".dado" / "config.yaml").exists() and not (root / ".dado" / "work" / "index.yaml").exists()
    if fresh or is_layout2(root):
        initialize_layout(root)
        installed, conflicts = install(root, force=args.force)
        Store(root).init()
    else:
        Store(root).init()
        installed, conflicts = install(root, force=args.force)
    sync_managed_active_profile(root)
    gitignore = root / ".dado" / ".gitignore"
    if not gitignore.exists():
        atomic_write(gitignore, "# If DADO data is versioned, exclude large/transient evidence.\nevidence/**/*.log\nevidence/**/*.tmp\ncache/\n*.tmp\n")
    print(f"Initialized DADO in {root}")
    for item in installed:
        print(f"  installed {item}")
    for item in conflicts:
        print(f"  preserved conflict: {item}")
    print("User-owned existing .opencode files and DADO work artifacts were preserved." if not args.force else "DADO-managed templates were refreshed with --force; user-owned files were preserved.")
    _profile_notice(root)
    if not shutil_which("opencode"):
        print("WARNING: OpenCode CLI was not found; project artifacts were initialized but agent integration cannot run yet.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    if not store.index_path.exists() and not store.root.exists():
        print("DADO is not initialized. Run `dado init`.")
        return 1
    works = store.list_active()
    index = read_yaml(store.index_path)
    work_id = args.id or index.get("focused")
    if not works:
        print("No active work.")
        return 0
    if not work_id:
        print(f"Active work: {len(works)} (no focused work)")
        for item in works:
            print(f"  {item['id']} — {item['title']} [{item['status']}]")
        return 0
    path = store.work_path(work_id)
    work = store.load_work(path)
    tasks = store.load_tasks(path)
    from collections import Counter
    counts = Counter(t["status"] for t in tasks)
    reqs = work.get("requirements", [])
    print(f"Work: {work_id}\nTitle: {work['title']}\nStatus: {work['status']}")
    print(f"Requirements: {len(reqs)}\nSatisfied:    {sum(r['status'] == 'done' for r in reqs)}")
    print("Tasks:")
    for name in ("done", "ready", "running", "review", "blocked", "failed", "stale", "pending", "cancelled"):
        print(f"  {name:<10} {counts.get(name, 0)}")
    next_tasks = store.ready(work_id, _max_parallel(store.project))
    print("Next:")
    print("\n".join(f"  {t['id']} {t['title']}" for t in next_tasks) or "  (none)")
    return 0


def _cmd_work_list(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    focused_id = store._validate_index().get("focused")
    for item in store.list_active():
        focused = " *" if item["id"] == focused_id else ""
        print(f"{item['id']} [{item['status']}] {item['title']}{focused}")
    return 0


def _cmd_work_show(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    path = store.work_path(args.id)
    work = store.load_work(path)
    tasks = store.load_tasks(path)
    _print_json({"work": work, "tasks": tasks, "artifacts": [p.name for p in path.iterdir() if p.is_file()]})
    return 0


def _cmd_work_create(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else args.request
    path = Store(root).create_work(args.title, request, args.id)
    print(f"Created {path.name}: {path}")
    return 0


def _read_packet(source: str) -> dict[str, Any]:
    label = "stdin" if source == "-" else source
    try:
        packet = yaml.safe_load(sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError) as exc:
        raise ValidationError(f"Cannot read YAML packet from {label}: {exc}") from exc
    if not isinstance(packet, dict):
        raise ValidationError(f"Packet {label} must contain one YAML mapping")
    return packet


def _cmd_task_add(args: argparse.Namespace) -> int:
    packet = _read_packet(args.packet)
    Store(project_root(args.project)).add_task(args.id, packet)
    print(f"Added {packet['id']} to {args.id}")
    return 0


def _cmd_task_state(args: argparse.Namespace) -> int:
    Store(project_root(args.project)).set_task_state(args.id, args.task, args.state)
    print(f"{args.task}: {args.state}")
    return 0


def _cmd_ready(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    ready = Store(root).ready(args.id, _max_parallel(root, args.max_parallel))
    _print_json(ready)
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    result = _read_packet(args.packet)
    Store(project_root(args.project)).record_result(args.id, result)
    print(f"Recorded result for {result['task']}; state is now {('review' if result['status'] == 'completed' else result['status'])}.")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    if args.changed and not args.paths_checked:
        raise ValidationError("--changed requires --paths-checked")
    checked = (args.changed or []) if args.paths_checked else None
    Store(project_root(args.project)).verify_task(args.id, args.task, args.verdict, args.check or [], args.evidence or [], checked,
                                                 captured_records=getattr(args, "record", None))
    print(f"{args.task}: independent verification {args.verdict}")
    return 0


def _cmd_invalidate(args: argparse.Namespace) -> int:
    affected = Store(project_root(args.project)).invalidate(args.id, args.task, args.reason)
    print("Marked stale: " + ", ".join(affected))
    return 0


def _cmd_requirement(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    if args.action == "add":
        entry = {"id": args.req, "description": args.description, "status": "pending", "source": args.source, "acceptance": args.acceptance or []}
        store.add_requirement(args.id, entry)
    else:
        store.set_requirement_status(args.id, args.req, args.status, args.reason)
    print(f"Updated {args.req}")
    return 0


def _cmd_archive(args: argparse.Namespace) -> int:
    path = Store(project_root(args.project)).archive(args.id)
    print(f"Archived work at {path}")
    return 0


def _cmd_link_commit(args: argparse.Namespace) -> int:
    sha = Store(project_root(args.project)).link_commit(args.id, args.commit)
    print(f"Linked {sha} to archived work {args.id}")
    return 0


def _cmd_work_state(args: argparse.Namespace) -> int:
    Store(project_root(args.project)).set_work_status(args.id, args.status)
    print(f"{args.id}: {args.status}")
    return 0


def _cmd_work_focus(args: argparse.Namespace) -> int:
    Store(project_root(args.project)).focus_work(args.id)
    print(f"Focused work: {args.id}")
    return 0


def _cmd_work_approve(args: argparse.Namespace) -> int:
    Store(project_root(args.project)).approve_work(args.id, args.note)
    print(f"Plan approval recorded for {args.id}; artifact fingerprint stored.")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8") if args.file else args.text
    Store(project_root(args.project)).promote_knowledge(args.id, args.target, text, args.source)
    print(f"Promoted sourced knowledge to .dado/work/project/{args.target}")
    return 0


def _cmd_decision(args: argparse.Namespace) -> int:
    decision_id = Store(project_root(args.project)).record_decision(args.id, args.text, args.rationale, args.requirement, args.task)
    print(f"Recorded {decision_id}")
    return 0


class _InteractiveCancelled(Exception):
    """The user opted out before saving an interactive model change."""


def _choice(prompt: str, choices: list[str], labels: list[str] | None = None) -> str:
    for index, value in enumerate(choices, 1):
        label = labels[index - 1] if labels else value
        print(f"  {index}. {label}")
    answer = input(prompt).strip()
    if answer.lower() in {"0", "q", "quit"}:
        raise _InteractiveCancelled
    if answer.isdigit() and 1 <= int(answer) <= len(choices):
        return choices[int(answer) - 1]
    if answer in choices:
        return answer
    raise ValidationError("Invalid selection")


def _interactive_roles() -> list[str]:
    answer = input("Roles (comma-separated numbers or 'all'; 0/q/quit cancels): ").strip()
    if answer.lower() in {"0", "q", "quit"}:
        raise _InteractiveCancelled
    if answer.lower() == "all" or answer == str(len(ROLES) + 1):
        return list(ROLES)
    values = [value.strip() for value in answer.split(",")]
    if not answer or any(not value for value in values):
        raise ValidationError("Select one or more roles by number, or enter 'all'")
    if any(value.lower() == "all" or value == str(len(ROLES) + 1) for value in values):
        raise ValidationError("Select 'all' by itself")
    selected: set[str] = set()
    for value in values:
        if not value.isdigit() or not 1 <= int(value) <= len(ROLES):
            raise ValidationError(f"Invalid DADO role number: {value}")
        selected.add(ROLES[int(value) - 1])
    return [role for role in ROLES if role in selected]


def _model_default_variant(variants: tuple[str, ...], model: str) -> str | None:
    if not variants:
        print(f"Reasoning variants: none advertised for {model}; using the model default.")
        return None
    print(f"Reasoning variants for {model}:")
    print("  1. model default (no explicit variant)")
    for index, variant in enumerate(variants, 2):
        print(f"  {index}. {variant}")
    answer = input("Reasoning variant (number or ID; blank uses model default; 0/q/quit cancels): ").strip()
    if answer.lower() in {"0", "q", "quit"}:
        raise _InteractiveCancelled
    if answer == "" or answer == "1" or answer.lower() in {"default", "model default"}:
        return None
    if answer in variants:
        return answer
    if answer.isdigit() and 2 <= int(answer) <= len(variants) + 1:
        return variants[int(answer) - 2]
    raise ValidationError(f"Unknown reasoning variant for {model}: {answer}")


def _display_profile_model(spec: dict[str, str | None]) -> str:
    model = spec["model"] or "inherit OpenCode default"
    variant = spec["variant"] or "model default"
    return f"{model}; reasoning variant: {variant}"


def _profile_notice(root: Path) -> None:
    from .layout import is_layout2
    if is_layout2(root):
        print("Personal profiles stay local. Launch with `dado host exec` to apply them; `dado host check` verifies the overlay without model calls.")


def _interactive_models(args: argparse.Namespace, root: Path, profile_name: str,
                        fixed_roles: list[str] | None) -> int:
    _require_role_agents(root)
    store = Store(root)
    config_path = root / ".dado" / "config.yaml"
    # Preview migrations and new profiles in memory only. In particular, quitting
    # before confirmation must not initialize config or project state on disk.
    config = store.load_config() if config_path.exists() else {}
    initialize_profiles(config, root / ".opencode" / "agents")
    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or DEFAULT_PROFILE not in profiles:
        raise ValidationError("DADO model profiles are not initialized; run `dado init`")
    default = validate_profile(profiles[DEFAULT_PROFILE])
    current = validate_profile(profiles.get(profile_name, default))

    print(f"Target profile: {profile_name}")
    print("DADO roles and their current model/reasoning variant:")
    for index, role in enumerate(ROLES, 1):
        print(f"  {index}. {role} — {_display_profile_model(current[role])}")
    print(f"  {len(ROLES) + 1}. all roles")
    roles = fixed_roles if fixed_roles is not None else _interactive_roles()

    structured = discover_model_catalog(root, args.opencode)
    providers = list(structured)
    print("Enabled providers:")
    provider = _choice("Provider (number or ID; 0/q/quit cancels): ", providers)
    model_ids = list(structured[provider])
    print(f"Enabled models for {provider}:")
    model_id = _choice("Model (number or ID; 0/q/quit cancels): ", model_ids)
    model = f"{provider}/{model_id}"
    variant = _model_default_variant(structured[provider][model_id], model)

    selection = {"model": model, "variant": variant}
    print(f"\nSave model selection to profile {profile_name}?")
    for role in roles:
        print(f"  {role}: {_display_profile_model(current[role])} -> {_display_profile_model(selection)}")
    answer = input("Save these changes? [y/N] (0/q/quit cancels): ").strip().lower()
    if answer in {"0", "q", "quit", "", "n", "no"}:
        raise _InteractiveCancelled
    if answer not in {"y", "yes"}:
        raise ValidationError("Expected yes or no")

    # This is the first point at which initialization or profile/agent writes are
    # permitted. edit_profile also keeps inactive profile edits off agent files.
    store.init()
    available = [f"{provider_id}/{item}" for provider_id, models in structured.items() for item in models]
    edit_profile(root, profile_name, {role: selection for role in roles}, available)
    print(f"Updated profile {profile_name}: {', '.join(roles)}")
    _profile_notice(root)
    return 0


def _cmd_models(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    if args.roles is not None and not args.roles:
        raise ValidationError("--roles requires at least one DADO agent name")
    profile_name = args.profile or DEFAULT_PROFILE
    validate_profile_name(profile_name)
    selected_roles = args.roles or list(ROLES)
    unknown = [role for role in selected_roles if role not in ROLES]
    if unknown:
        raise ValidationError(f"Unknown DADO agent: {unknown[0]}")
    requested_variant = args.variant
    requested_model = args.model
    model_catalog_variant = None
    if requested_model and requested_model != "inherit" and "#" in requested_model:
        requested_model, _, model_catalog_variant = requested_model.partition("#")
        if requested_variant not in (None, "none"):
            raise ValidationError("Specify a variant either in --model or --variant, not both")
    if requested_model and requested_model != "inherit" and not re.fullmatch(r"[^/\s#]+/[^\s#]+", requested_model):
        raise ValidationError(f"Invalid model reference: {requested_model!r}")
    if model_catalog_variant is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", model_catalog_variant):
        raise ValidationError("Invalid Variant ID in model reference")
    if requested_variant not in (None, "none") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", requested_variant):
        raise ValidationError("Invalid Variant ID")
    if requested_model == "inherit" and requested_variant not in (None, "none"):
        raise ValidationError("Cannot choose a variant when inheriting the session model")
    if not args.list and not requested_model and requested_variant is None:
        try:
            return _interactive_models(args, root, profile_name, args.roles)
        except _InteractiveCancelled:
            print("Cancelled; no profile or agent changes made.")
            return 0
    available = catalog(args.opencode)
    if requested_model and requested_model != "inherit" and requested_model not in {item.split("#", 1)[0] for item in available}:
        raise ValidationError(f"Model is not in current OpenCode catalog: {requested_model}")
    print("Models currently available from OpenCode:")
    for idx, model in enumerate(available, 1):
        print(f"  {idx}. {model}")
    if args.list:
        return 0
    _require_role_agents(root)
    store = Store(root)
    store.init()
    config = store.load_config()
    profile = config["profiles"].get(profile_name, empty_profile())
    if requested_model or requested_variant is not None:
        if requested_model == "inherit" and requested_variant not in (None, "none"):
            raise ValidationError("Cannot choose a variant when inheriting the session model")
        pending: dict[str, dict[str, Any]] = {}
        for role in selected_roles:
            model = (None if requested_model == "inherit" else requested_model) if requested_model else KEEP_MODEL
            if requested_variant == "none":
                variant = None
            elif requested_variant is not None:
                variant = requested_variant
            elif model_catalog_variant is not None:
                variant = model_catalog_variant
            else:
                variant = KEEP_VARIANT
            pending[role] = {"model": model, "variant": variant}
        edit_profile(root, profile_name, pending, available)
        print(f"Updated profile {profile_name}: {', '.join(selected_roles)}")
        _profile_notice(root)
        return 0
    print("Choose per-agent model and optional variant (model-specific ID). Enter `i` to inherit or `q` to quit.")
    pending: dict[str, dict[str, str | None]] = {}
    for role in selected_roles:
        existing = profile[role]
        value = input(f"{role} [{existing['model'] or 'inherit'}]: ").strip()
        if value.lower() == "q":
            print("No changes made.")
            return 0
        if value.lower() == "i":
            pending[role] = {"model": None, "variant": None}
        elif not value and existing["model"]:
            pending[role] = {"model": existing["model"], "variant": existing["variant"]}
        else:
            try:
                index = int(value)
                if not 1 <= index <= len(available):
                    raise ValueError
                chosen, _, catalog_variant = available[index - 1].partition("#")
                old_variant = existing["variant"] if existing["model"] == chosen else None
                value = input(f"{role} variant (blank keeps {old_variant or catalog_variant or 'model default'}, '-' clears): ").strip()
                variant = None if value == "-" else value or old_variant or catalog_variant or None
                pending[role] = {"model": chosen, "variant": variant}
            except (ValueError, IndexError):
                raise ValidationError(f"Invalid selection for {role}")
    edit_profile(root, profile_name, pending, available)
    print(f"Updated profile {profile_name}. OpenCode checks whether each variant is supported when used.")
    return 0


def _cmd_profiles(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    if args.action == "use":
        validate_profile_name(args.name)
        _require_role_agents(root)
    store = Store(root)
    store.init()
    if args.action == "list":
        active, profiles = list_profiles(root)
        print(f"Active profile: {active}")
        print("Available profiles: " + ", ".join(profiles))
        return 0
    _, profiles = list_profiles(root)
    if args.name not in profiles:
        raise ValidationError(f"Unknown model profile: {args.name}")
    available = catalog(args.opencode)
    use_profile(root, args.name, available)
    from .layout import is_layout2
    if is_layout2(root):
        print(f"Active local profile: {args.name}. A new `dado host exec` invocation applies it; existing sessions/private servers keep their current overlay.")
    else:
        print(f"Active model profile: {args.name}. The selection applies to later agent invocations, not the current in-flight model.")
    _profile_notice(root)
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    errors = []
    # Inspect recovery independently of index validity: a partial archive can
    # temporarily leave the index pointing at files already moved by its journal.
    from .persistence import inventory
    for journal_root in (root / ".dado", root / ".dado" / "work"):
        for entry in inventory(journal_root):
            print(f"Journal {entry['phase']}: {entry['journal']} (inspect `dado repair`)")
            errors.append(f"Unresolved {entry['phase']} journal: {entry['journal']}")
    opencode = shutil_which("opencode")
    available = []
    print(f"OpenCode: {opencode or 'not found'}")
    if opencode:
        try:
            version = subprocess.check_output([opencode, "--version"], text=True, timeout=10).strip()
            print("Version: " + version)
            import re
            match = re.search(r"(?:v)?(\d+)\.(\d+)\.(\d+)", version)
            if not match or tuple(map(int, match.groups())) < (2, 0, 0):
                errors.append(f"OpenCode V2 is required; detected {version!r}")
            available = catalog(opencode)
            print("Model catalog: available")
        except (DadoError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            errors.append(str(exc))
    try:
        store = Store(root)
        items = store.list_active()
        store.load_config()
        print(f"DADO index: valid ({len(items)} active work)")
        indexed = {item["id"] for item in items}
        for directory in store.active.iterdir():
            if directory.is_dir() and directory.name not in indexed:
                print(f"WARNING orphan active work directory: {directory.name} (not in index; preserve and inspect manually)")
        for item in items:
            path = store.work_path(item["id"])
            store.load_work(path)
            tasks = store.load_tasks(path)
            running = [t["id"] for t in tasks if t["status"] == "running"]
            if running:
                print(f"WARNING abandoned/uncertain running tasks in {item['id']}: {', '.join(running)} (run dado resume {item['id']})")
            for task in tasks:
                result = store._result(path, task)
                verification = store._verdict(path, task)
                if result:
                    store._schema_validate("result", result)
                if verification:
                    store._schema_validate("verification", verification)
                if task["status"] == "done" and (verification or {}).get("verdict") != "PASS":
                    raise ValidationError(f"{task['id']} is done without durable verifier PASS")
                if task["status"] == "review" and not result:
                    raise ValidationError(f"{task['id']} is in review without a worker result")
            print(f"DAG {item['id']}: valid ({len(tasks)} tasks)")
    except DadoError as exc:
        errors.append(str(exc))
    for role in ROLES:
        file = root / ".opencode" / "agents" / f"{role}.md"
        if not file.exists():
            print(f"Agent {role}: missing")
            errors.append(f"DADO agent is not installed: {role}; run `dado init`")
            continue
        try:
            text = file.read_text(encoding="utf-8")
            if not text.startswith("---\n") or "\n---" not in text[4:]:
                raise ValidationError("missing or malformed YAML frontmatter")
            finish = text.find("\n---", 4)
            front = yaml.safe_load(text[4:finish]) or {}
            model = front.get("model")
            if model and available and model.split("#", 1)[0] not in {entry.split("#", 1)[0] for entry in available}:
                raise ValidationError(f"configured model is not in current `opencode models`: {model}")
            print(f"Agent {role}: valid" + (f" ({model})" if model else " (inherits OpenCode default)"))
        except (yaml.YAMLError, DadoError, AttributeError) as exc:
            errors.append(f"Agent {role}: {exc}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Doctor: no blocking issues found.")
    return 0


def shutil_which(cmd: str) -> str | None:
    import shutil
    return shutil.which(cmd)


def _cmd_validate(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    for item in store.list_active():
        tasks = store.load_tasks(store.work_path(item["id"]))
        validate_dag(tasks)
        print(f"{item['id']}: valid ({len(tasks)} tasks)")
    return 0


def _cmd_host(args: argparse.Namespace) -> int:
    from . import host
    root = project_root(args.project)
    if args.action == "check":
        _print_json(host.check(root, args.opencode))
        return 0
    arguments = args.arguments
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return host.launch(root, arguments, args.opencode)


def _cmd_handoff(args: argparse.Namespace) -> int:
    from .handoff import export_work, import_work
    store = Store(project_root(args.project))
    if args.action == "export":
        _print_json(export_work(store, args.id, Path(args.file), args.evidence))
    else:
        print(f"Imported new local execution: {import_work(store, Path(args.file)).name}; approval and verification required.")
    return 0


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from .evaluation import main as evaluate
    return evaluate(args.arguments)


def _cmd_policy(args: argparse.Namespace) -> int:
    from .policy import main as policy
    return policy(args.arguments)


def _cmd_isolate(args: argparse.Namespace) -> int:
    from .isolation import main as isolate
    return isolate(args.arguments)


def _cmd_check(args: argparse.Namespace) -> int:
    from .evidence import capture_check
    from .verification import integration_criteria, snapshot_scopes
    store = Store(project_root(args.project))
    path = store.work_path(args.id)
    subject = args.task or args.id
    scopes = None
    if args.task:
        task = next((task for task in store.load_tasks(path) if task["id"] == args.task), None)
        if task is None:
            raise ValidationError(f"Unknown task {args.task}")
        criteria = task["acceptance"]
        scopes = snapshot_scopes(store.project, task["scope"]["allowed"])
    else:
        criteria = integration_criteria(store.load_work(path))
    if any(criterion not in criteria for criterion in args.criterion):
        raise ValidationError("--criterion must exactly name an acceptance criterion (work: 'REQ-NNN: criterion')")
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not command:
        raise ValidationError("Supply an explicit command after --")
    record = capture_check(store.project, path / "evidence" / "checks", command,
                           {"subject": subject, "acceptance": args.criterion, "exit_code": 0},
                           timeout=args.timeout, output_cap_bytes=args.output_cap, scopes=scopes,
                           session={"id": args.session_id, "source": "caller-declared"} if args.session_id else {})
    reference = f"evidence/checks/{record['id']}.json"
    _print_json({"record": reference, "exit_code": record["observed"]["exit_code"],
                 "timed_out": record["observed"]["timed_out"], "criterion_met": record["result"]["criterion_met"]})
    return 0 if record["result"]["criterion_met"] is True and not record["observed"]["timed_out"] else 1


def _cmd_work_verify(args: argparse.Namespace) -> int:
    from .fs import file_lock
    from .verification import record_integration
    store = Store(project_root(args.project))
    with file_lock(store.root / ".state"):
        _print_json(record_integration(store, store.work_path(args.id), args.record))
    return 0


def _cmd_budget(args: argparse.Namespace) -> int:
    from . import budget
    store = Store(project_root(args.project))
    path = store.work_path(args.id) / "budget.yaml"
    if args.action == "init":
        result = budget.initialize(path, cost=args.cost, time_minutes=args.minutes, retries=args.retries)
    elif args.action == "status":
        result = budget.status(path)
    elif args.action == "release":
        result = budget.update(path, "release", reservation_id=args.reservation)
    else:
        arguments = {"cost": args.cost, "time_minutes": args.minutes, "retries": args.retries}
        arguments["phase" if args.action == "reserve" else "reservation_id"] = args.task if args.action == "reserve" else args.reservation
        result = budget.update(path, args.action, **arguments)
    _print_json(result)
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from .layout import migrate
    _print_json(migrate(project_root(args.project), apply=args.apply))
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    from .layout import repair_layout
    root = project_root(args.project)
    _print_json({"configuration": repair_layout(root, apply=args.apply),
                 "work": Store(root).repair(apply=args.apply)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dado", description="Durable Artifact-Driven Orchestration")
    parser.add_argument("--version", action="version", version=f"dado {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, function in (("migrate", _cmd_migrate), ("repair", _cmd_repair)):
        p = sub.add_parser(name, help="inspect changes by default; --apply executes explicitly")
        p.add_argument("--project"); p.add_argument("--apply", action="store_true")
        p.set_defaults(func=function)
    check = sub.add_parser("check", help="capture an explicitly requested check against current code")
    check.add_argument("id"); check.add_argument("--project"); check.add_argument("--task")
    check.add_argument("--criterion", action="append", required=True)
    check.add_argument("--timeout", type=float, default=120)
    check.add_argument("--output-cap", type=int, default=1000000)
    check.add_argument("--session-id")
    check.add_argument("argv", nargs="+", help="explicit command after --"); check.set_defaults(func=_cmd_check)
    budget = sub.add_parser("budget", help="durable opt-in admission reservations; not provider billing")
    bs = budget.add_subparsers(dest="action", required=True)
    for name in ("init", "status", "reserve", "charge", "release"):
        p = bs.add_parser(name); p.add_argument("id"); p.add_argument("--project")
        if name in {"init", "reserve", "charge"}:
            p.add_argument("--cost", type=float)
            p.add_argument("--minutes", type=float, default=90 if name == "init" else 0)
            p.add_argument("--retries", type=int, default=3 if name == "init" else 0)
        if name == "reserve": p.add_argument("--task", required=True)
        if name in {"charge", "release"}: p.add_argument("reservation")
        p.set_defaults(func=_cmd_budget)
    for name, function, help_text in (("evaluate", _cmd_evaluate, "offline benchmark reports; no model calls"),
                                      ("policy", _cmd_policy, "explain routing and approved budgets"),
                                      ("isolate", _cmd_isolate, "isolated local snapshots and explicit integration")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("arguments", nargs=argparse.REMAINDER)
        p.set_defaults(func=function)
    host = sub.add_parser("host", help="personal model overlay without editing shared agents")
    hs = host.add_subparsers(dest="action", required=True)
    for name in ("check", "exec"):
        p = hs.add_parser(name)
        p.add_argument("--project"); p.add_argument("--opencode", default="opencode")
        if name == "exec":
            p.add_argument("arguments", nargs=argparse.REMAINDER, help="OpenCode arguments after --; private server enforced")
        p.set_defaults(func=_cmd_host)
    handoff = sub.add_parser("handoff", help="explicit portable work export/import")
    hs = handoff.add_subparsers(dest="action", required=True)
    for name in ("export", "import"):
        p = hs.add_parser(name)
        p.add_argument("--project")
        if name == "export":
            p.add_argument("id"); p.add_argument("--evidence", action="append", default=[])
        p.add_argument("file"); p.set_defaults(func=_cmd_handoff)
    init = sub.add_parser("init", help="initialize DADO safely in this project")
    init.add_argument("--project"); init.add_argument("--force", action="store_true", help="overwrite changed DADO-managed templates (use with care)")
    init.set_defaults(func=_cmd_init)
    status = sub.add_parser("status", help="compact status of focused work")
    status.add_argument("id", nargs="?"); status.add_argument("--project"); status.set_defaults(func=_cmd_status)
    doctor = sub.add_parser("doctor", help="check installation, artifacts, and abandoned state")
    doctor.add_argument("--project"); doctor.set_defaults(func=_cmd_doctor)
    validate = sub.add_parser("validate", help="validate active work and DAGs")
    validate.add_argument("--project"); validate.set_defaults(func=_cmd_validate)
    models = sub.add_parser("models", help="select real OpenCode models for DADO agents")
    models.add_argument("--project"); models.add_argument("--opencode", default="opencode"); models.add_argument("--list", action="store_true")
    models.add_argument("--model", help="model ID or inherit"); models.add_argument("--variant", help="model-specific variant ID, or none to clear"); models.add_argument("--roles", nargs="*")
    models.add_argument("--profile", help="profile to create/edit (default: default; never changes the active profile unless it is active)")
    models.set_defaults(func=_cmd_models)
    profiles = sub.add_parser("profiles", help="list or activate named model profiles")
    ps = profiles.add_subparsers(dest="action", required=True)
    p = ps.add_parser("list", help="show the active and available model profiles")
    p.add_argument("--project"); p.set_defaults(func=_cmd_profiles)
    p = ps.add_parser("use", help="activate a model profile for later agent invocations")
    p.add_argument("name"); p.add_argument("--project"); p.add_argument("--opencode", default="opencode"); p.set_defaults(func=_cmd_profiles)
    resume = sub.add_parser("resume", help="reconcile interrupted work")
    resume.add_argument("id"); resume.add_argument("--project"); resume.set_defaults(func=lambda a: _resume(a))
    work = sub.add_parser("work", help="inspect and manage work")
    ws = work.add_subparsers(dest="work_command", required=True)
    p = ws.add_parser("verify", help="record captured integration acceptance")
    p.add_argument("id"); p.add_argument("--project"); p.add_argument("--record", action="append", required=True)
    p.set_defaults(func=_cmd_work_verify)
    for name, fn in (("list", _cmd_work_list), ("show", _cmd_work_show), ("create", _cmd_work_create), ("ready", _cmd_ready), ("archive", _cmd_archive)):
        p = ws.add_parser(name)
        p.add_argument("--project")
        if name in {"show", "ready", "archive"}: p.add_argument("id")
        if name == "create":
            p.add_argument("title"); p.add_argument("--request"); p.add_argument("--request-file"); p.add_argument("--id")
        if name == "ready": p.add_argument("--max-parallel", type=int)
        p.set_defaults(func=fn)
    p = ws.add_parser("state"); p.add_argument("id"); p.add_argument("status", choices=["discovery", "planning", "awaiting_approval", "executing", "blocked", "completed", "cancelled"]); p.add_argument("--project"); p.set_defaults(func=_cmd_work_state)
    p = ws.add_parser("focus"); p.add_argument("id"); p.add_argument("--project"); p.set_defaults(func=_cmd_work_focus)
    p = ws.add_parser("approve"); p.add_argument("id"); p.add_argument("--note", required=True, help="record concise user approval text"); p.add_argument("--project"); p.set_defaults(func=_cmd_work_approve)
    p = ws.add_parser("link-commit", help="link an existing Git commit to a completed archived work")
    p.add_argument("id"); p.add_argument("commit"); p.add_argument("--project"); p.set_defaults(func=_cmd_link_commit)
    task = sub.add_parser("task", help="manage structured task packets")
    ts = task.add_subparsers(dest="task_command", required=True)
    for cmd in ("add", "state", "result", "invalidate", "verify"):
        p = ts.add_parser(cmd); p.add_argument("id"); p.add_argument("--project")
        if cmd == "add": p.add_argument("packet", help="packet YAML file or - for stdin")
        elif cmd == "state": p.add_argument("task"); p.add_argument("state", choices=["ready", "running", "review", "blocked", "failed", "cancelled", "stale"])
        elif cmd == "result": p.add_argument("packet", help="result YAML file or - for stdin")
        elif cmd == "verify":
            p.add_argument("task"); p.add_argument("verdict", choices=["PASS", "FAIL", "BLOCKED"]); p.add_argument("--check", action="append"); p.add_argument("--evidence", action="append")
            p.add_argument("--paths-checked", action="store_true", help="verifier independently inspected changed paths (including none)")
            p.add_argument("--changed", action="append", help="independently observed changed path; requires --paths-checked")
            p.add_argument("--record", action="append", help="captured check record relative to this work")
        else: p.add_argument("task"); p.add_argument("reason")
        p.set_defaults(func={"add": _cmd_task_add, "state": _cmd_task_state, "result": _cmd_result, "invalidate": _cmd_invalidate, "verify": _cmd_verify}[cmd])
    req = sub.add_parser("requirement", help="record spec requirement state")
    rs = req.add_subparsers(dest="action", required=True)
    p = rs.add_parser("add"); p.add_argument("id"); p.add_argument("req"); p.add_argument("description"); p.add_argument("--source", choices=["user", "discovered", "inferred"], default="user"); p.add_argument("--acceptance", action="append"); p.add_argument("--project"); p.set_defaults(func=_cmd_requirement)
    p = rs.add_parser("state"); p.add_argument("id"); p.add_argument("req"); p.add_argument("status", choices=["pending", "done", "blocked", "waived"]); p.add_argument("--reason"); p.add_argument("--project"); p.set_defaults(func=_cmd_requirement)
    promote = sub.add_parser("promote", help="explicitly promote sourced durable knowledge to project memory")
    promote.add_argument("id"); promote.add_argument("target", choices=["architecture.md", "conventions.md", "constraints.md", "decisions.md"])
    promote.add_argument("--source", required=True, help="origin artifact such as DEC-004")
    group = promote.add_mutually_exclusive_group(required=True); group.add_argument("--text"); group.add_argument("--file")
    promote.add_argument("--project"); promote.set_defaults(func=_cmd_promote)
    decision = sub.add_parser("decision", help="record an auditable architecture/work decision")
    ds = decision.add_subparsers(dest="action", required=True)
    p = ds.add_parser("add"); p.add_argument("id"); p.add_argument("--text", required=True); p.add_argument("--rationale", required=True)
    p.add_argument("--requirement", action="append"); p.add_argument("--task", action="append"); p.add_argument("--project"); p.set_defaults(func=_cmd_decision)
    un = sub.add_parser("uninstall", help="remove unchanged managed OpenCode files; preserve work data")
    un.add_argument("--project"); un.add_argument("--remove-config", action="store_true"); un.add_argument("--remove-data", action="store_true", help="DESTRUCTIVE: also remove .dado/work after confirmation"); un.set_defaults(func=_cmd_uninstall)
    update = sub.add_parser("update", help="update unmodified DADO-managed OpenCode templates")
    update.add_argument("--project"); update.add_argument("--force", action="store_true", help="overwrite modified managed templates")
    update.set_defaults(func=_cmd_init)
    return parser


def _resume(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    changed = store.recover(args.id)
    print("Reconciled: " + (", ".join(changed) if changed else "no interrupted tasks"))
    _cmd_status(argparse.Namespace(project=args.project, id=args.id))
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    if args.remove_data:
        answer = input("This permanently deletes .dado/work including archived user artifacts. Type DELETE WORK to confirm: ")
        if answer != "DELETE WORK":
            print("Cancelled; work data preserved.")
            args.remove_data = False
    if not (root / ".dado" / "managed-files.json").exists():
        print("WARNING: managed-file manifest is missing; DADO files cannot be safely identified for removal. Inspect .opencode manually.")
    removed, preserved = uninstall(root, args.remove_config, args.remove_data)
    print("Removed: " + (", ".join(removed) if removed else "none"))
    print("Preserved modified files: " + (", ".join(preserved) if preserved else "none"))
    return 0


def main() -> None:
    try:
        parser = build_parser()
        args = parser.parse_args()
        # --request is optional only when --request-file is supplied.
        if args.command == "work" and args.work_command == "create" and not args.request and not args.request_file:
            raise ValidationError("Provide --request or --request-file")
        raise SystemExit(args.func(args))
    except DadoError as exc:
        print(f"dado: error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except (OSError, yaml.YAMLError, ValueError, KeyError, TypeError, EOFError) as exc:
        print(f"dado: error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
