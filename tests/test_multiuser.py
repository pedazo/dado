"""Phase 0 multi-user layout: shared/local split, migration and safeguards.

Covers (ADR 0005):
- two clones keep independent personality (profiles) while sharing policies;
- policy keys are rejected in local state, so shared policies cannot be
  relaxed per checkout;
- migration is dry-run-reporting, leaves local backups, and is idempotent;
- tracked operational files are reported as diagnostics and tracked agents
  are never modified (actionable error, no dirty files, no silent skip);
- ``.dado/.gitignore`` is generated safely (append-only, user lines kept);
- installer preserves user-owned files and never writes through symlinks.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from dado import layout, models
from dado.errors import ConflictError, ValidationError
from dado.fs import file_lock
from dado.installer import install, uninstall
from dado.store import Store
from dado.transactions import FileTransaction

AVAILABLE = ["opencode-go/glm-5.3-flash", "opencode-go/glm-5.3-flash#high"]

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "dado-test",
    "GIT_AUTHOR_EMAIL": "dado-test@example.invalid",
    "GIT_COMMITTER_NAME": "dado-test",
    "GIT_COMMITTER_EMAIL": "dado-test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
}


def git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True, env=_GIT_ENV, cwd=cwd)


def digests(project: Path, paths: list[Path]) -> str:
    return hashlib.sha256(b"\0".join(path.read_bytes() for path in paths)).hexdigest()


def all_agent_files(project: Path) -> list[Path]:
    return [project / ".opencode" / "agents" / f"{role}.md" for role in models.ROLES]


def legacy_project(tmp_path: Path, name: str, *, repo: bool = False) -> Path:
    project = tmp_path / name
    project.mkdir()
    Store(project).init()
    install(project)
    if repo:
        git("init", "-q", cwd=project)
        git("add", "-A", ".dado", cwd=project)
        git("commit", "-qm", "legacy dado shared state", cwd=project)
    return project


def clone(source: Path, target: Path) -> Path:
    git("clone", "-q", str(source), str(target))
    return target


def role_agents_tracked(project: Path) -> bool:
    out = subprocess.run(
        ["git", "ls-files", ".opencode/agents"], cwd=project,
        capture_output=True, text=True, check=True,
    ).stdout
    return bool(out.strip())


# ------------------------------------------------------------- legacy mode

def test_no_layout_marker_keeps_legacy_profile_behavior(tmp_path):
    project = legacy_project(tmp_path, "legacy")
    profiles_path = layout.config_path_for_profiles(project)
    assert profiles_path == project / ".dado" / "config.yaml"
    assert layout.is_layout2(project) is False
    config = layout.load_effective_config(project)
    assert config["profiles"] and config["active_profile"] == "default"
    assert "verification" in config  # legacy single file carries everything

    # Legacy edits remain in the shared file exactly as before.
    models.edit_profile(project, "edgy", {
        "dado-worker": {"model": AVAILABLE[0], "variant": None},
    }, AVAILABLE)
    reused = models._load_config(project)
    assert "edgy" in reused["profiles"]
    assert (project / ".dado" / "config.local.yaml").exists() is False


def test_write_effective_config_and_load_roundtrip_legacy(tmp_path):
    project = legacy_project(tmp_path, "legacy2")
    config = layout.load_effective_config(project)
    config["verification"] = {"default_risk": "high"}
    written = layout.write_effective_config(project, config)
    assert set(written) == {layout.SHARED_CONFIG_NAME}
    reloaded = layout.load_effective_config(project)
    assert reloaded["verification"] == {"default_risk": "high"}
    assert reloaded["profiles"] == config["profiles"]


# ---------------------------------------------------------------- migration

def test_migrate_dry_run_reports_without_changes(tmp_path):
    project = legacy_project(tmp_path, "legacy3")
    before = (project / ".dado" / "config.yaml").read_bytes()
    report = layout.migrate(project, dry_run=True)
    assert report["status"] == "dry-run"
    assert report["backup_preview"]
    assert (project / ".dado" / "config.yaml").read_bytes() == before
    assert layout.is_layout2(project) is False
    assert (project / ".dado" / "config.local.yaml").exists() is False
    assert report["extracted_profiles"] == ["default"]
    assert set(report["shared_policies"]) <= set(layout.POLICY_KEYS)


def test_migrate_apply_backs_up_extracts_and_is_idempotent(tmp_path):
    project = legacy_project(tmp_path, "legacy4")
    original = (project / ".dado" / "config.yaml").read_bytes()

    report = layout.migrate(project, apply=True)
    assert report["status"] == "migrated"
    dado = project / ".dado"
    assert layout.is_layout2(project)
    # Shared file: policies only.
    import yaml
    shared = yaml.safe_load((dado / "config.yaml").read_text())
    assert set(shared) == (set(shared) & set(layout.POLICY_KEYS))
    assert shared["verification"]["default_risk"] == "medium"
    # Local file: profiles and active profile only.
    local = yaml.safe_load((dado / "config.local.yaml").read_text())
    assert set(local) == {"profiles", "active_profile"}
    assert "default" in local["profiles"]
    # Marker carries the project UUID.
    marker = layout.read_layout(project)
    assert marker["version"] == layout.LAYOUT_VERSION
    assert layout.project_uuid(project) == marker["project_uuid"]
    # Backup preserved the exact legacy bytes locally.
    backup = Path(report["backup"])
    assert backup.is_file()
    assert backup.read_bytes() == original
    assert Path(report["backup_manifest"]).is_file()
    # Local identity exists and is ignored.
    assert layout.checkout_uuid(project)
    assert (dado / "identity").exists()

    # Effective view equals the legacy content.
    config = layout.load_effective_config(project)
    assert config["profiles"] == yaml.safe_load(original.decode())["profiles"]
    assert config["active_profile"] == "default"

    backups = list((dado / layout.BACKUP_ROOT).iterdir())
    second = layout.migrate(project, apply=True)
    assert second["status"] == "already"
    assert list((dado / layout.BACKUP_ROOT).iterdir()) == backups  # no new backup
    assert all(path.exists() for path in all_agent_files(project))


def test_migrate_refuses_tracked_local_config_on_apply_and_report_on_dry_run(tmp_path):
    project = legacy_project(tmp_path, "legacy5", repo=True)
    (project / ".dado" / "config.local.yaml").write_text("profiles: {}\n")
    git("add", ".dado/config.local.yaml", cwd=project)
    git("commit", "-qm", "track local-ish file", cwd=project)

    dry = layout.migrate(project, dry_run=True)
    assert f".dado/{layout.LOCAL_CONFIG_NAME}" in dry["tracked"]["tracked_local_files"]
    assert dry["status"] == "dry-run"
    with pytest.raises(ConflictError, match="git rm --cached"):
        layout.migrate(project, apply=True)
    assert layout.is_layout2(project) is False  # nothing moved


def test_initialize_layout_refuses_legacy_profiles_without_migration(tmp_path):
    project = legacy_project(tmp_path, "legacy6")
    with pytest.raises(ConflictError, match="dado migrate"):
        layout.initialize_layout(project)
    migrated = legacy_project(tmp_path, "legacy7")
    layout.migrate(migrated, apply=True)
    again = layout.initialize_layout(migrated)
    assert again["version"] == layout.LAYOUT_VERSION


# ------------------------------------------------------ shared/local split

def test_local_config_rejects_policy_keys_and_shared_rejects_profiles(tmp_path):
    project = legacy_project(tmp_path, "split1")
    layout.migrate(project, apply=True)
    local_path = layout.local_config_path(project)
    shared_path = layout.shared_config_path(project)
    good_local_text = local_path.read_text()

    # A checkout cannot relax shared policies locally.
    local_path.write_text("profiles:\n  x: {}\nactive_profile: x\nverification:\n  default_risk: low\n")
    with pytest.raises(ValidationError, match="verification"):
        layout.load_effective_config(project)

    text = shared_path.read_text()
    shared_path.write_text(text + "profiles:\n  smuggled: {}\nactive_profile: smuggled\n")
    with pytest.raises(ValidationError, match="profiles"):
        layout.load_effective_config(project)
    shared_path.write_text(text)
    shared_path.write_text(shared_path.read_text() + "mystery_key: true\n")
    with pytest.raises(ValidationError, match="mystery_key"):
        layout.load_effective_config(project)
    shared_path.write_text(text)  # restore both halves before loading a valid config
    local_path.write_text(good_local_text)

    # write_effective_config routes halves correctly: no policies in local.
    config = layout.load_effective_config(project)
    config["scheduler"] = {"max_parallel_workers": 2}
    layout.write_effective_config(project, config)
    local_text = local_path.read_text()
    assert "scheduler" not in local_text and "verification" not in local_text
    assert config["scheduler"] in yaml_like(shared_path.read_text()).values()


def yaml_like(text: str):
    import yaml
    return yaml.safe_load(text) or {}


def test_two_clones_share_policies_but_have_independent_personality(tmp_path):
    base = legacy_project(tmp_path, "origin", repo=True)
    # Migrate the origin and share the result: layout marker, policies-only
    # config and generated .dado/.gitignore get committed; local profile
    # state and identity stay untracked and are rebuilt per clone.
    assert layout.migrate(base, apply=True)["status"] == "migrated"
    git("add", "-A", ".dado", cwd=base)
    git("commit", "-qm", "adopt shared/local layout", cwd=base)

    first = clone(base, tmp_path / "clone-one")
    second = clone(base, tmp_path / "clone-two")
    # Inherited marker: no re-migration needed; checkout identity differs.
    assert layout.migrate(first, apply=True)["status"] == "already"
    assert layout.migrate(second, apply=True)["status"] == "already"
    assert layout.project_uuid(first) == layout.project_uuid(second)
    assert layout.checkout_uuid(first) != layout.checkout_uuid(second)
    # Fresh clones inherit neither local profile state nor local identity...
    assert (layout.local_config_path(first)).exists() is False
    assert (layout.local_config_path(second)).exists() is False
    # ...and the working tree ships no tracked personal state.
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=first, capture_output=True, text=True, check=True
    ).stdout
    assert [line.split()[-1] for line in status.splitlines() if "agents/" in line] == []

    # The documented per-clone setup: install agents locally, seed the local
    # profile registry (wired `dado init` flow), then bind profiles purely
    # through local state (v2 never rewrites agent files).
    install(first)
    install(second)
    assert models.seed_local_profiles(first) is True
    assert models.seed_local_profiles(second) is True
    assert models.seed_local_profiles(first) is False  # idempotent

    # Personality: a new profile lives only in clone one's local file.
    models.edit_profile(first, "fast", {
        "dado-worker": {"model": AVAILABLE[0], "variant": None},
    }, AVAILABLE)
    models.use_profile(first, "fast", AVAILABLE)
    local = yaml_like(layout.local_config_path(first).read_text())
    assert local["active_profile"] == "fast"
    assert "fast" in local["profiles"]
    # Policies stay shared and identical between clones.
    assert yaml_like(layout.shared_config_path(first).read_text()) == yaml_like(
        layout.shared_config_path(second).read_text())
    # Clone two keeps its seeded defaults: no "fast" profile, not activated.
    second_local = yaml_like(layout.local_config_path(second).read_text())
    assert "fast" not in second_local["profiles"]
    assert second_local["active_profile"] == "default"
    # Agent files stay template-pure in both clones (host wrapper projects).
    assert models.agent_model(first / ".opencode" / "agents" / "dado-worker.md") is None
    assert models.agent_model(second / ".opencode" / "agents" / "dado-worker.md") is None
    status_after = subprocess.run(
        ["git", "status", "--porcelain", ".opencode", ".dado"], cwd=first,
        capture_output=True, text=True, check=True,
    ).stdout
    assert [line.split()[-1] for line in status_after.splitlines() if "agents/" in line] == []


# --------------------------------------------------------- tracked agents

def test_layout2_never_modifies_agent_files_even_tracked(tmp_path):
    project = legacy_project(tmp_path, "tracked1", repo=True)
    git("add", ".opencode", cwd=project)
    git("commit", "-qm", "track agents", cwd=project)
    assert role_agents_tracked(project)

    report = layout.migrate(project, apply=True)
    assert set(report["tracked"]["tracked_role_agents"]) == set(models.ROLES)
    assert f".dado/{layout.SHARED_CONFIG_NAME}" in report["tracked"]["tracked_dado"]

    agent_paths = all_agent_files(project)
    before = digests(project, agent_paths)
    before_local = layout.local_config_path(project).read_bytes()

    # Layout2: activation is local state only; the model reaches OpenCode via
    # the host wrapper, so even tracked agents stay untouched.
    models.edit_profile(project, "fast", {
        "dado-worker": {"model": AVAILABLE[0], "variant": None},
    }, AVAILABLE)
    models.use_profile(project, "fast", AVAILABLE)
    local = yaml_like(layout.local_config_path(project).read_text())
    assert local["active_profile"] == "fast"
    assert local["profiles"]["fast"]["dado-worker"] == {"model": AVAILABLE[0], "variant": None}

    assert digests(project, agent_paths) == before  # agent files are template-pure
    assert layout.migrate(project, apply=True)["status"] == "already"
    status = subprocess.run(
        ["git", "status", "--porcelain", ".opencode"], cwd=project,
        capture_output=True, text=True, check=True,
    ).stdout
    assert status.strip() == ""  # no substituted model clobbered anything

    # Wiring seam for the host wrapper: entries feed dado.host.environment.
    from dado.host import environment
    env = environment(models.active_profile_spec(project), {"SENTINEL": "keep"})
    agents = json.loads(env["OPENCODE_CONFIG_CONTENT"])["agents"]
    assert agents["dado-worker"]["model"] == AVAILABLE[0]
    assert "dado-explorer" not in agents  # model None: inherit shared default
    assert env["SENTINEL"] == "keep"


def test_layout2_profile_writes_use_the_journal(tmp_path, monkeypatch):
    project = legacy_project(tmp_path, "journal1")
    assert layout.migrate(project, apply=True)["status"] == "migrated"

    calls: dict[str, object] = {}

    class FakeTx:
        def __init__(self, root, roots, label):
            calls["root"] = Path(root).name
            calls["roots"] = [Path(r).name for r in roots]
            calls["label"] = label
            calls["writes"] = []

        @classmethod
        def begin(cls, root, roots=None, *, label=""):
            return cls(root, roots, label)

        def write(self, path, content):
            calls["writes"].append(Path(path).name)

        def apply(self):
            calls["applied"] = True
            return {"id": "fake", "phase": "committed", "applied": len(calls["writes"])}

    monkeypatch.setattr(models, "FileTransaction", FakeTx)
    models.use_profile(project, "default", AVAILABLE)
    assert calls["applied"] is True
    assert calls["writes"] == [layout.LOCAL_CONFIG_NAME]
    assert calls["roots"] == [".dado"]  # state-only writes; agents untouched
    local = yaml_like(layout.local_config_path(project).read_text())
    assert local["active_profile"] == "default"


# ------------------------------------------------------------- .gitignore

def test_gitignore_generated_safely_and_preserves_user_lines(tmp_path):
    project = legacy_project(tmp_path, "ignore1")
    layout.migrate(project, apply=True)
    ignore = project / ".dado" / ".gitignore"
    text = ignore.read_text()
    for entry in layout.GITIGNORE_ENTRIES:
        assert entry in text
    assert layout.ensure_gitignore(project) is False  # idempotent, no rewrite churn
    stripped = text.replace("backups/\n", "")
    user_line = stripped + "\ncustom-user-file.txt\n"
    ignore.write_text(user_line)
    assert layout.ensure_gitignore(project) is True  # adds only the missing entry
    after = ignore.read_text()
    assert "custom-user-file.txt" in after  # user content kept
    assert "backups/" in after  # missing entry restored, nothing else rewritten
    assert after.count(layout.GITIGNORE_HEADER) == 1


# ---------------------------------------------------------------- installer

def test_installer_preserves_user_owned_symlinks_without_escape(tmp_path, symlink_capable):
    project = legacy_project(tmp_path, "inst1")
    user_agent = project / ".opencode" / "agents" / "dado-explorer.md"
    user_content = user_agent.read_text()
    user_agent.write_text("---\ndescription: mine\n---\nUser content.\n")
    # Drop the managed entry so the file is genuinely user-owned.
    import json
    manifest_path = project / ".dado" / "managed-files.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["agents/dado-explorer.md"]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    # Managed target replaced with a symlink pointing OUTSIDE the project.
    victim = tmp_path / "escape-target.md"
    victim.write_text("outside\n")
    staged = project / ".opencode" / "agents" / "dado-worker.md"
    staged_content = staged.read_text()
    staged.unlink()
    staged.symlink_to(victim)

    installed, conflicts = install(project)
    assert conflicts, "expected conflicts for user-owned and symlink targets"
    assert user_agent.read_text() == "---\ndescription: mine\n---\nUser content.\n"
    assert victim.read_text() == "outside\n"  # no write-through the escape
    assert staged.is_symlink()  # symlink untouched
    assert str(staged) in "\n".join(conflicts)
    assert "user-owned file preserved" in "\n".join(conflicts)

    # Uninstall refuses to remove through the symlink.
    removed, preserved = uninstall(project)
    assert staged.is_symlink()
    assert any("symbolic link" in entry for entry in preserved)
    # Restore for cleanliness.
    staged.unlink()
    staged.write_text(staged_content)


def test_installer_refuses_opencode_symlink_escape(tmp_path, symlink_capable):
    project = legacy_project(tmp_path, "inst2")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    project.joinpath(".opencode").rename(elsewhere / "agents-home")
    link = project / ".opencode"
    link.symlink_to(elsewhere / "agents-home", target_is_directory=True)
    with pytest.raises(ConflictError, match="outside the project"):
        install(project)


# --------------------------------------------------------- installer manifest

def test_managed_manifest_is_local_and_compatible_paths(tmp_path):
    project = legacy_project(tmp_path, "inst3")
    import json
    manifest = project / ".dado" / "managed-files.json"
    data = json.loads(manifest.read_text())
    assert all(not key.startswith("/") and ".." not in Path(key).parts for key in data)
    assert all(key.startswith("agents/") or key.startswith("commands/") or "skills/" in key for key in data)
    layout.migrate(project, apply=True)
    assert "managed-files.json" in (project / ".dado" / ".gitignore").read_text()


# ------------------------------------------------- strict gate + diagnostics

def test_layout_marker_rejects_unknown_or_malformed(tmp_path):
    project = legacy_project(tmp_path, "mark1")
    (project / ".dado" / "layout.yaml").write_text("version: 99\n")
    with pytest.raises(ConflictError, match="Unsupported layout marker"):
        layout.is_layout2(project)
    with pytest.raises(ConflictError, match="Unsupported layout marker"):
        layout.migrate(project, apply=True)
    (project / ".dado" / "layout.yaml").write_text("not a mapping at all\n")
    with pytest.raises(ConflictError, match="Malformed"):
        layout.is_layout2(project)
    # Legacy projects without a marker are untouched by the strictness.
    untouched = legacy_project(tmp_path, "mark2")
    assert layout.is_layout2(untouched) is False


def test_layout2_reads_require_shared_config_with_explicit_bootstrap(tmp_path):
    project = legacy_project(tmp_path, "strict1")
    assert layout.migrate(project, apply=True)["status"] == "migrated"
    (project / ".dado" / "config.yaml").unlink()  # v2 shared half gone
    with pytest.raises(ValidationError, match="missing"):
        layout.load_effective_config(project)  # never silently relaxes policies
    config = layout.load_effective_config(project, bootstrap=True)  # explicit seed path
    assert config["profiles"] and config["active_profile"] == "default"


def test_gitignore_covers_local_runtime_and_imports(tmp_path):
    project = legacy_project(tmp_path, "ignore2")
    layout.migrate(project, apply=True)
    text = layout.gitignore_path(project).read_text()
    assert "local/" in text and "work/imports/" in text and "*.lock" in text


def test_tracked_operational_classification_separates_shared_work(tmp_path):
    project = legacy_project(tmp_path, "trackclass", repo=True)
    arranged = {
        ".dado/local/isolation/registry.json": "{}\n",
        ".dado/work/active/W-a/work.yaml": "id: W-a\n",
        ".dado/work/archive/2026/W-b/work.yaml": "id: W-b\n",
        ".dado/work/events.jsonl": "{}\n",
        ".dado/work/imports/W-c/paper.json": "{}\n",
        ".dado/backups/x/config.yaml": "x\n",
        ".dado/work/project/architecture.md": "# shared\n",
        ".dado/work/project/conventions.md": "# shared\n",
    }
    for rel, text in arranged.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git("add", "-A", ".dado", cwd=project)
    git("commit", "-qm", "classify", cwd=project)
    inv = layout.tracked_operational_files(project)
    for rel in (".dado/work/project/architecture.md", ".dado/work/project/conventions.md"):
        assert rel in inv["tracked_shared_work"]
    assert {".dado/work/project/architecture.md", ".dado/work/project/conventions.md"} <= set(
        inv["tracked_shared_work"]) and not any("active" in entry for entry in inv["tracked_shared_work"])
    assert inv["tracked_operational"] == sorted(
        set(inv["tracked_dado"]) - set(inv["tracked_shared_work"]))
    for rel in (".dado/local/isolation/registry.json", ".dado/work/events.jsonl",
                ".dado/work/imports/W-c/paper.json", ".dado/backups/x/config.yaml",
                ".dado/work/active/W-a/work.yaml", ".dado/work/archive/2026/W-b/work.yaml"):
        assert rel in inv["tracked_local_files"], rel


# --------------------------------------------------------- migration guards

def test_migrate_refuses_divergent_existing_local_config(tmp_path):
    project = legacy_project(tmp_path, "div1")
    local_path = layout.local_config_path(project)
    malicious = "profiles:\n  user: {}\nactive_profile: user\n"
    local_path.write_text(malicious)  # untracked user preference
    with pytest.raises(ConflictError, match="divergent"):
        layout.migrate(project, apply=True)
    assert local_path.read_text() == malicious  # never overwritten
    assert layout.is_layout2(project) is False  # marker untouched
    assert not list((project / ".dado" / layout.BACKUP_ROOT).glob("*"))


def test_migrate_apply_is_serialized_on_model_profiles_lock(tmp_path, monkeypatch):
    project = legacy_project(tmp_path, "locked1")
    monkeypatch.setattr(layout, "LOCK_TIMEOUT", 0.3)
    lockpath = project / ".dado" / ".model-profiles"
    entered, release = threading.Event(), threading.Event()

    def holder():
        with file_lock(lockpath):
            entered.set()
            release.wait()

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ConflictError, match="lock"):
            layout.migrate(project, apply=True)
        assert layout.is_layout2(project) is False  # blocked, not half-applied
    finally:
        release.set()
        thread.join()
    assert layout.migrate(project, apply=True)["status"] == "migrated"  # serializes fine


# -------------------------------------------------------------------- repair

def test_repair_layout_replays_pending_and_never_building(tmp_path):
    project = legacy_project(tmp_path, "reap1")
    root = project / ".dado"
    dst = project / ".opencode" / "repair-probe.md"

    crashed = FileTransaction.begin(root, roots=[root, project / ".opencode"], label="crash")
    crashed.write(dst, "replayed\n")
    crashed.seal()  # pending: replayable

    builder = FileTransaction.begin(root, roots=[root], label="interrupted")
    builder.write(root / "building-probe.txt", "nope\n")  # building: never sealed

    report = layout.repair_layout(project)  # report-only
    phases = {entry["id"]: entry for entry in report["journals"]}
    assert all(entry["phase"] in {"building", "pending", "committed"} for entry in report["journals"])
    assert phases[crashed.id]["action"].startswith("replayable")
    assert phases[builder.id]["action"].startswith("inspection-only")
    assert all("committed" != phases[m]["phase"] or "already applied" in phases[m]["action"]
               for m in phases)
    assert report["replayed"] == 0 and dst.exists() is False

    applied = layout.repair_layout(project, apply=True)
    assert applied["replayed"] == 1
    assert dst.read_text() == "replayed\n"
    building_probe = root / "building-probe.txt"
    assert not building_probe.exists()  # building journals are never replayed
    assert builder.id in applied["building"]
    # Malformed journals are reported, not crashing the repair.
    (root / ".txn" / "junk.json").write_text("{not json")
    degraded = layout.repair_layout(project, apply=True)
    assert any(entry["phase"] == "unreadable" for entry in degraded["journals"])


def test_journal_is_mandatory_for_layout2_writes(tmp_path, monkeypatch):
    project = legacy_project(tmp_path, "nostore1")
    assert layout.migrate(project, apply=True)["status"] == "migrated"
    before = layout.local_config_path(project).read_bytes()
    monkeypatch.setattr(models, "FileTransaction", None)
    monkeypatch.setattr(layout, "FileTransaction", None)
    with pytest.raises(ConflictError, match="journal unavailable"):
        models.use_profile(project, "default", AVAILABLE)
    assert layout.local_config_path(project).read_bytes() == before
    config = layout.load_effective_config(project)
    with pytest.raises(ConflictError, match="journal unavailable"):
        layout.write_effective_config(project, config)
    assert json.loads(json.dumps(config)) == config  # sane: no writes happened


# ------------------------------------------------- accepted init order (no Store edits)

def test_accepted_initialization_order_on_fresh_project(tmp_path):
    project = tmp_path / "fresh"
    project.mkdir()
    # 1) CLI init step 1: layout initialization before Store.init.
    marker = layout.initialize_layout(project)
    assert marker["version"] == layout.LAYOUT_VERSION and layout.is_layout2(project)
    # 2) agent install (untracked templates).
    install(project)
    # 3) Store wiring writes the shared policies half (policies only).
    policies = {
        "approval": {"plan": True},
        "scheduler": {"max_parallel_workers": 3},
        "verification": {"default_risk": "medium"},
        "history": {"automatic_search": False},
        "archive": {"completed": True},
    }
    layout.write_effective_config(project, policies)
    assert "profiles" not in (project / ".dado" / "config.yaml").read_text()
    # 4) seed the local profile registry (per clone).
    assert models.seed_local_profiles(project) is True
    assert models.seed_local_profiles(project) is False
    config = layout.load_effective_config(project)
    assert config["profiles"]["default"]["dado-worker"] == {"model": None, "variant": None}
    assert config["verification"] == {"default_risk": "medium"}
    # 5) personal assignment is local only, projected via the host wrapper.
    models.edit_profile(project, "fast", {
        "dado-worker": {"model": AVAILABLE[0], "variant": None},
    }, AVAILABLE)
    models.use_profile(project, "fast", AVAILABLE)
    from dado.host import environment
    agents = json.loads(environment(models.active_profile_spec(project), {})["OPENCODE_CONFIG_CONTENT"])["agents"]
    assert agents["dado-worker"]["model"] == AVAILABLE[0]
    assert "dado-explorer" not in agents  # model None: inherit shared default
    assert models.agent_model(project / ".opencode" / "agents" / "dado-worker.md") is None
