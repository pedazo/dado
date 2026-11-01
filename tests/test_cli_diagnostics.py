from argparse import Namespace
import subprocess

import pytest

from dado.cli import _cmd_status, _read_packet, project_root
from dado.errors import DadoError
from dado.store import Store


def test_nested_initialized_project_wins_over_git_parent(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    nested = tmp_path / "nested"
    Store(nested).init()
    monkeypatch.chdir(nested)
    assert project_root() == nested
    assert project_root(str(tmp_path)) == tmp_path


def test_status_distinguishes_corruption_from_missing_installation(tmp_path, capsys):
    args = Namespace(project=str(tmp_path), id=None)
    assert _cmd_status(args) == 1
    assert "not initialized" in capsys.readouterr().out
    store = Store(tmp_path)
    store.init()
    store.index_path.write_text("active: [", encoding="utf-8")
    with pytest.raises(DadoError):
        _cmd_status(args)
    assert "not initialized" not in capsys.readouterr().out


def test_invalid_packet_identifies_source(tmp_path):
    packet = tmp_path / "packet.yaml"
    packet.write_text("depends_on: [", encoding="utf-8")
    with pytest.raises(DadoError, match="packet.yaml"):
        _read_packet(str(packet))
