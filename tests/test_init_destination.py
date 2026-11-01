from __future__ import annotations

import subprocess
from pathlib import Path

from dado import cli


def _run(monkeypatch, cwd: Path, command: str, project: str | None = None) -> int:
    monkeypatch.chdir(cwd)
    argv = [command]
    if project is not None:
        argv += ["--project", project]
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def test_init_in_non_git_child_does_not_discover_or_touch_ancestor_project(tmp_path, monkeypatch):
    ancestor = tmp_path / "ancestor"
    child = ancestor / "proyecto-sin-git"
    child.mkdir(parents=True)
    (ancestor / ".dado").mkdir()
    (ancestor / ".dado/config.yaml").write_text("user-owned parent config\n")
    (ancestor / ".opencode/agents").mkdir(parents=True)
    (ancestor / ".opencode/agents/custom.md").write_text("user-owned parent agent\n")
    monkeypatch.setattr(cli, "shutil_which", lambda command: "/usr/bin/opencode")

    assert _run(monkeypatch, child, "init") == 0
    assert (child / ".dado/config.yaml").is_file()
    assert (child / ".opencode/agents/dado-master.md").is_file()
    assert (ancestor / ".dado/config.yaml").read_text() == "user-owned parent config\n"
    assert (ancestor / ".opencode/agents/custom.md").read_text() == "user-owned parent agent\n"
    assert not (ancestor / ".opencode/agents/dado-master.md").exists()


def test_init_inside_git_subdirectory_stays_at_current_directory(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    child = repository / "nested"
    child.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    monkeypatch.setattr(cli, "shutil_which", lambda command: "/usr/bin/opencode")

    assert _run(monkeypatch, child, "init") == 0
    assert (child / ".dado/config.yaml").is_file()
    assert not (repository / ".dado/config.yaml").exists()


def test_init_explicit_project_is_expanded_and_resolved(tmp_path, monkeypatch):
    home = tmp_path / "home"
    destination = home / "chosen-project"
    destination.mkdir(parents=True)
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(cli, "shutil_which", lambda command: "/usr/bin/opencode")

    assert _run(monkeypatch, cwd, "init", "~/chosen-project") == 0
    assert (destination / ".dado/config.yaml").is_file()
    assert not (cwd / ".dado/config.yaml").exists()


def test_update_keeps_existing_ancestor_project_discovery(tmp_path, monkeypatch):
    ancestor = tmp_path / "ancestor"
    child = ancestor / "nested"
    child.mkdir(parents=True)
    (ancestor / ".dado").mkdir()
    monkeypatch.setattr(cli, "shutil_which", lambda command: "/usr/bin/opencode")

    assert _run(monkeypatch, child, "update") == 0
    assert (ancestor / ".dado/config.yaml").is_file()
    assert not (child / ".dado/config.yaml").exists()
