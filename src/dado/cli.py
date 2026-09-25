"""DADO command-line interface."""
from __future__ import annotations

import argparse
import json
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
from .models import ROLES, catalog, set_agent_model
from .store import Store


def project_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    try:
        root = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True, stderr=subprocess.DEVNULL).strip()
        return Path(root).resolve()
    except (OSError, subprocess.CalledProcessError):
        current = Path.cwd().resolve()
        for candidate in (current, *current.parents):
            if (candidate / ".dado").is_dir() or (candidate / ".opencode").is_dir():
                return candidate
        return current


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False))


def _max_parallel(root: Path, override: int | None = None) -> int:
    if override is not None:
        return max(1, override)
    config = Store(root).load_config()
    return max(1, int(config.get("scheduler", {}).get("max_parallel_workers", 3)))


def _template_schema_copy(project: Path) -> list[str]:
    import shutil
    schema_dir = Path(__file__).parent / "schemas"
    destination = project / ".dado" / "schemas"
    written = []
    destination.mkdir(parents=True, exist_ok=True)
    for schema in schema_dir.glob("*.json"):
        target = destination / schema.name
        if not target.exists():
            shutil.copyfile(schema, target)
            written.append(str(target.relative_to(project)))
    return written


def _cmd_init(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    root.mkdir(parents=True, exist_ok=True)
    Store(root).init()
    installed, conflicts = install(root, force=args.force)
    schemas = _template_schema_copy(root)
    gitignore = root / ".dado" / ".gitignore"
    if not gitignore.exists():
        atomic_write(gitignore, "# Keep structured work artifacts in Git; omit large/transient evidence.\nevidence/**/*.log\nevidence/**/*.tmp\ncache/\n*.tmp\n")
    print(f"Initialized DADO in {root}")
    for item in installed + schemas:
        print(f"  installed {item}")
    for item in conflicts:
        print(f"  preserved conflict: {item}")
    print("User-owned existing .opencode files and DADO work artifacts were preserved." if not args.force else "DADO-managed templates were refreshed with --force; user-owned files were preserved.")
    if not shutil_which("opencode"):
        print("WARNING: OpenCode CLI was not found; project artifacts were initialized but agent integration cannot run yet.")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    try:
        works = store.list_active()
    except DadoError:
        print("DADO is not initialized. Run `dado init`.")
        return 1
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
    for item in store.list_active():
        focused = " *" if item["id"] == read_yaml(store.index_path).get("focused") else ""
        print(f"{item['id']} [{item['status']}] {item['title']}{focused}")
    return 0


def _cmd_work_show(args: argparse.Namespace) -> int:
    store = Store(project_root(args.project))
    path = store.work_path(args.id)
    work = store.load_work(path)
    tasks = store.load_tasks(path)
    _print_json({"work": work, "requirements": work.get("requirements", []), "tasks": tasks, "artifacts": ["request.md", "spec.md", "decisions.md", "plan.yaml"]})
    return 0


def _cmd_work_create(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    request = Path(args.request_file).read_text(encoding="utf-8") if args.request_file else args.request
    path = Store(root).create_work(args.title, request, args.id)
    print(f"Created {path.name}: {path}")
    return 0


def _cmd_task_add(args: argparse.Namespace) -> int:
    packet = yaml.safe_load(Path(args.packet).read_text(encoding="utf-8"))
    if not isinstance(packet, dict):
        raise ValidationError("Task packet YAML must contain one mapping")
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
    result = yaml.safe_load(Path(args.packet).read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValidationError("Result YAML must contain one mapping")
    Store(project_root(args.project)).record_result(args.id, result)
    print(f"Recorded result for {result['task']}; state is now {('review' if result['status'] == 'completed' else result['status'])}.")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    Store(project_root(args.project)).verify_task(args.id, args.task, args.verdict, args.check or [], args.evidence or [])
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


def _cmd_models(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    if args.roles is not None and not args.roles:
        raise ValidationError("--roles requires at least one DADO agent name")
    agents = root / ".opencode" / "agents"
    available = catalog(args.opencode)
    missing_agents = [role for role in ROLES if not (agents / f"{role}.md").is_file()]
    if missing_agents:
        raise ValidationError(f"DADO agents are missing: {', '.join(missing_agents)}; run `dado init`")
    print("Models currently available from OpenCode:")
    for idx, model in enumerate(available, 1):
        print(f"  {idx}. {model}")
    if args.list:
        return 0
    if args.model:
        selected_roles = args.roles or list(ROLES)
        for role in selected_roles:
            if role not in ROLES:
                raise ValidationError(f"Unknown DADO agent: {role}")
            path = agents / f"{role}.md"
            if not path.exists():
                raise ValidationError(f"Agent is not installed: {path}; run `dado init`")
            set_agent_model(path, None if args.model == "inherit" else args.model, available)
        print(f"Updated {', '.join(selected_roles)} to {args.model}")
        return 0
    print("Choose per-agent model. Enter a catalog number, `i` to inherit OpenCode default, or `q` to quit.")
    pending: dict[str, str | None] = {}
    for role in ROLES:
        value = input(f"{role}: ").strip()
        if value.lower() == "q":
            print("No changes made.")
            return 0
        if value.lower() == "i":
            pending[role] = None
        else:
            try:
                index = int(value)
                if not 1 <= index <= len(available):
                    raise ValueError
                pending[role] = available[index - 1]
            except (ValueError, IndexError):
                raise ValidationError(f"Invalid selection for {role}")
    for role, model in pending.items():
        set_agent_model(agents / f"{role}.md", model, available)
    print("Updated agent frontmatter. No separate DADO model registry was created.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = project_root(args.project)
    errors = []
    opencode = shutil_which("opencode")
    available = []
    print(f"OpenCode: {opencode or 'not found'}")
    if opencode:
        try:
            version = subprocess.check_output([opencode, "--version"], text=True).strip()
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
                result = read_yaml(path / "results" / f"{task['id']}.yaml")
                verification = read_yaml(path / "verification" / f"{task['id']}.yaml")
                if result is not None:
                    store._schema_validate("result", result)
                if verification is not None:
                    store._schema_validate("verification", verification)
                if task["status"] == "done" and (verification or {}).get("verdict") != "PASS":
                    raise ValidationError(f"{task['id']} is done without durable verifier PASS")
                if task["status"] == "review" and result is None:
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
            if model and available and model not in available:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dado", description="Durable Artifact-Driven Orchestration")
    parser.add_argument("--version", action="version", version=f"dado {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
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
    models.add_argument("--model", help="model ID or inherit"); models.add_argument("--roles", nargs="*")
    models.set_defaults(func=_cmd_models)
    resume = sub.add_parser("resume", help="reconcile interrupted work")
    resume.add_argument("id"); resume.add_argument("--project"); resume.set_defaults(func=lambda a: _resume(a))
    work = sub.add_parser("work", help="inspect and manage work")
    ws = work.add_subparsers(dest="work_command", required=True)
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
    task = sub.add_parser("task", help="manage structured task packets")
    ts = task.add_subparsers(dest="task_command", required=True)
    for cmd in ("add", "state", "result", "invalidate", "verify"):
        p = ts.add_parser(cmd); p.add_argument("id"); p.add_argument("--project")
        if cmd == "add": p.add_argument("packet")
        elif cmd == "state": p.add_argument("task"); p.add_argument("state", choices=["ready", "running", "review", "blocked", "failed", "cancelled", "stale"])
        elif cmd == "result": p.add_argument("packet")
        elif cmd == "verify":
            p.add_argument("task"); p.add_argument("verdict", choices=["PASS", "FAIL", "BLOCKED"]); p.add_argument("--check", action="append"); p.add_argument("--evidence", action="append")
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
