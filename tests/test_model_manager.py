from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from dado import cli, models
from dado.errors import DadoError
from dado.installer import install
from dado.models import ROLES, agent_model
from dado.store import Store


def _api_payload(project: Path, data: list[dict]) -> dict:
    return {"location": {"directory": str(project.resolve())}, "data": data}


def _ready_project(project: Path) -> None:
    Store(project).init()
    install(project)


def _input(monkeypatch, answers: list[str]) -> None:
    remaining = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(remaining))


def _run(project: Path, *flags: str) -> int:
    args = cli.build_parser().parse_args(["models", "--project", str(project), *flags])
    return args.func(args)


def test_structured_catalog_filters_disabled_models_and_keeps_api_variant_ids(tmp_path):
    payload = _api_payload(tmp_path, [
        {"providerID": "openai", "id": "active", "enabled": True,
         "variants": [{"id": "low"}, {"id": "adaptive:high"}]},
        {"providerID": "unused", "id": "disabled", "enabled": False},
    ])

    assert models.parse_model_catalog(payload, tmp_path) == {
        "openai": {"active": ("low", "adaptive:high")},
    }


@pytest.mark.parametrize("payload", [
    [],
    {"location": {"directory": "/different/project"}, "data": []},
    {"location": {"directory": "/project"}, "data": [{"enabled": "yes"}]},
    {"location": {"directory": "/project"}, "data": [
        {"providerID": "bad/provider", "id": "model", "enabled": True, "variants": []},
    ]},
    {"location": {"directory": "/project"}, "data": [
        {"providerID": "openai", "id": "bad", "enabled": True, "variants": [{"id": "bad id"}]},
    ]},
])
def test_structured_catalog_rejects_invalid_response_and_wrong_location(tmp_path, payload):
    if isinstance(payload, dict) and payload.get("location", {}).get("directory") in {"bad", "/project"}:
        payload = {**payload, "location": {"directory": str(tmp_path.resolve())}}
    with pytest.raises(DadoError):
        models.parse_model_catalog(payload, tmp_path)


def test_discovery_uses_location_scoped_api_and_timeout(tmp_path, monkeypatch):
    expected = _api_payload(tmp_path, [
        {"providerID": "provider", "id": "model", "enabled": True, "variants": []},
    ])
    invocation = {}

    class Process:
        returncode = 0

        def communicate(self, timeout=None):
            invocation["timeout"] = timeout
            return None, b""

    def fake_popen(command, **kwargs):
        invocation["command"] = command
        invocation["cwd"] = kwargs["cwd"]
        kwargs["stdout"].write(json.dumps(expected).encode())
        return Process()

    monkeypatch.setattr(models.subprocess, "Popen", fake_popen)
    result = models.discover_model_catalog(tmp_path, "opencode", timeout=7)
    endpoint = urlsplit(invocation["command"][-1])
    assert invocation["command"][:3] == ["opencode", "api", "get"]
    assert parse_qs(endpoint.query) == {"location[directory]": [str(tmp_path.resolve())]}
    assert invocation["cwd"] == tmp_path.resolve()
    assert invocation["timeout"] == 7
    assert result == {"provider": {"model": ()}}

    class TimedOut:
        pid = 123

        def communicate(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("opencode api get", timeout)
            return None, b""

        def kill(self):
            killed.append((self.pid, "process"))

    monkeypatch.setattr(models.subprocess, "Popen", lambda *a, **k: TimedOut())
    killed = []
    if models.os.name != "nt":
        monkeypatch.setattr(models.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(DadoError, match="timed out after 7 seconds"):
        models.discover_model_catalog(tmp_path, "opencode", timeout=7)
    assert killed


def test_interactive_manager_selects_multiple_roles_provider_model_and_advertised_variant(tmp_path, monkeypatch, capsys):
    _ready_project(tmp_path)
    before = {role: (tmp_path / f".opencode/agents/{role}.md").read_text() for role in ROLES}
    monkeypatch.setattr(cli, "discover_model_catalog", lambda project, executable: {
        "anthropic": {"claude": ("low",)},
        "openai": {"alpha": ("low", "adaptive:high"), "beta": ()},
    })
    _input(monkeypatch, ["1,3", "openai", "alpha", "adaptive:high", "yes"])

    assert _run(tmp_path) == 0
    output = capsys.readouterr().out
    assert "Target profile: default" in output
    assert "dado-master — inherit OpenCode default; reasoning variant: model default" in output
    assert "Enabled models for openai" in output
    assert "claude" not in output
    assert agent_model(tmp_path / ".opencode/agents/dado-master.md") == "openai/alpha#adaptive:high"
    assert agent_model(tmp_path / ".opencode/agents/dado-worker.md") == "openai/alpha#adaptive:high"
    assert Store(tmp_path).load_config()["profiles"]["default"]["dado-master"]["variant"] == "adaptive:high"
    for role in set(ROLES) - {"dado-master", "dado-worker"}:
        assert (tmp_path / f".opencode/agents/{role}.md").read_text() == before[role]


def test_interactive_manager_supports_all_roles_and_model_default(tmp_path, monkeypatch):
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "discover_model_catalog", lambda project, executable: {
        "provider": {"model": ("high", "low")},
    })
    _input(monkeypatch, ["all", "provider", "model", "1", "yes"])

    assert _run(tmp_path) == 0
    assert all(agent_model(tmp_path / f".opencode/agents/{role}.md") == "provider/model" for role in ROLES)


def test_interactive_manager_uses_model_default_when_no_variants_are_advertised(tmp_path, monkeypatch, capsys):
    _ready_project(tmp_path)
    monkeypatch.setattr(cli, "discover_model_catalog", lambda project, executable: {
        "provider": {"plain": ()},
    })
    _input(monkeypatch, ["2", "provider", "plain", "yes"])

    assert _run(tmp_path) == 0
    assert agent_model(tmp_path / ".opencode/agents/dado-explorer.md") == "provider/plain"
    assert "none advertised" in capsys.readouterr().out


@pytest.mark.parametrize("answers", [
    ["q"],
    ["1", "q"],
    ["1", "1", "q"],
    ["1", "1", "1", "q"],
    ["1", "1", "1", "2", "no"],
])
def test_cancellation_at_every_interactive_stage_makes_no_profile_or_agent_changes(tmp_path, monkeypatch, answers):
    _ready_project(tmp_path)
    config = tmp_path / ".dado/config.yaml"
    before_config = config.read_bytes()
    before_agents = {role: (tmp_path / f".opencode/agents/{role}.md").read_bytes() for role in ROLES}
    monkeypatch.setattr(cli, "discover_model_catalog", lambda project, executable: {
        "provider": {"model": ("high",)},
    })
    _input(monkeypatch, answers)

    assert _run(tmp_path, "--profile", "not-saved") == 0
    assert config.read_bytes() == before_config
    assert "not-saved" not in Store(tmp_path).load_config()["profiles"]
    assert {role: (tmp_path / f".opencode/agents/{role}.md").read_bytes() for role in ROLES} == before_agents


def test_cancel_does_not_initialize_missing_profile_config(tmp_path, monkeypatch):
    _ready_project(tmp_path)
    config = tmp_path / ".dado/config.yaml"
    config.unlink()
    before_agents = {role: (tmp_path / f".opencode/agents/{role}.md").read_bytes() for role in ROLES}
    _input(monkeypatch, ["q"])

    assert _run(tmp_path, "--profile", "uncreated") == 0
    assert not config.exists()
    assert {role: (tmp_path / f".opencode/agents/{role}.md").read_bytes() for role in ROLES} == before_agents


def test_interactive_inactive_profile_copies_default_without_projecting_agent_changes(tmp_path, monkeypatch):
    _ready_project(tmp_path)
    available = ["provider/default", "provider/active", "provider/custom"]
    models = {
        "dado-worker": {"model": "provider/default", "variant": "low"},
    }
    models.update({role: {"model": None, "variant": None} for role in ROLES if role not in models})
    from dado.models import edit_profile, use_profile
    edit_profile(tmp_path, "default", models, available)
    edit_profile(tmp_path, "active", {"dado-master": {"model": "provider/active", "variant": None}}, available)
    use_profile(tmp_path, "active", available)
    before_agents = {role: (tmp_path / f".opencode/agents/{role}.md").read_bytes() for role in ROLES}
    monkeypatch.setattr(cli, "discover_model_catalog", lambda project, executable: {
        "provider": {"custom": ("high",)},
    })
    _input(monkeypatch, ["3", "provider", "custom", "high", "yes"])

    assert _run(tmp_path, "--profile", "custom-profile") == 0
    config = Store(tmp_path).load_config()
    assert config["active_profile"] == "active"
    assert config["profiles"]["custom-profile"] == {
        **models,
        "dado-worker": {"model": "provider/custom", "variant": "high"},
    }
    assert {role: (tmp_path / f".opencode/agents/{role}.md").read_bytes() for role in ROLES} == before_agents
