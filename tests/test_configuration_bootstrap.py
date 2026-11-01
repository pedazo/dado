from dado import configuration
from dado.fs import read_yaml
from dado.layout import initialize_layout


def test_fresh_v2_bootstrap_separates_profiles_and_requires_integration(tmp_path):
    initialize_layout(tmp_path)
    config = configuration.initialize(tmp_path)
    shared = read_yaml(tmp_path / ".dado" / "config.yaml")
    personal = read_yaml(tmp_path / ".dado" / "config.local.yaml")
    assert "profiles" not in shared
    assert set(personal) == {"profiles", "active_profile"}
    assert config["verification"]["require_integration"] is True
    before = (tmp_path / ".dado" / "config.yaml").stat().st_mtime_ns
    assert configuration.initialize(tmp_path) == config
    assert (tmp_path / ".dado" / "config.yaml").stat().st_mtime_ns == before


def test_legacy_bootstrap_keeps_legacy_config_contract(tmp_path):
    config = configuration.initialize(tmp_path)
    assert read_yaml(tmp_path / ".dado" / "config.yaml") == config
    assert not (tmp_path / ".dado" / "config.local.yaml").exists()
    assert "require_integration" not in config["verification"]
