import json

import pytest

from dado.errors import ConflictError, ValidationError
from dado.handoff import export_work, import_work, load_bundle, _digest
from dado.store import Store


def source_work(root):
    store = Store(root)
    work = store.create_work("Portable contract", "Implement the agreed behavior")
    store.add_requirement(work.name, {"id": "REQ-001", "description": "Behavior", "status": "pending", "source": "user", "acceptance": ["Works"]})
    store.add_task(work.name, {"id": "T-001", "title": "Implement", "goal": "Implement", "satisfies": ["REQ-001"],
        "scope": {"allowed": ["src"], "forbidden": []}, "acceptance": ["Works"], "verification": ["Test"]})
    store.set_work_status(work.name, "awaiting_approval")
    store.approve_work(work.name, "Approved fixture")
    return store, work


def test_handoff_new_execution_never_reuses_approval(tmp_path):
    source, work = source_work(tmp_path / "alice")
    bundle = tmp_path / "handoff.json"
    export_work(source, work.name, bundle)
    target = Store(tmp_path / "bob")
    imported = import_work(target, bundle)
    assert target.load_work(imported)["status"] == "discovery"
    assert "approval" not in target.load_work(imported)
    assert target.load_tasks(imported)[0]["status"] == "pending"
    assert json.loads((imported / "handoff.json").read_text())["verification"] == "historical-only"
    with pytest.raises(ConflictError, match="already imported"):
        import_work(target, bundle)


def test_bundle_tampering_and_escape_rejected_before_import(tmp_path):
    source, work = source_work(tmp_path / "alice")
    bundle = tmp_path / "handoff.json"
    export_work(source, work.name, bundle)
    value = json.loads(bundle.read_text())
    value["payload"]["title"] = "tampered"
    bundle.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="corrupt"):
        load_bundle(bundle, Store(tmp_path / "bob"))
    value["payload"]["evidence"] = {"evidence/../../outside": {"base64": "", "sha256": ""}}
    value["sha256"] = _digest(value["payload"])
    bundle.write_text(json.dumps(value))
    with pytest.raises(ValidationError, match="under evidence"):
        load_bundle(bundle, Store(tmp_path / "bob"))
    assert not (tmp_path / "bob" / ".dado").exists()


def test_export_includes_only_explicit_evidence(tmp_path):
    source, work = source_work(tmp_path / "alice")
    (work / "evidence" / "check.txt").write_text("passed")
    (work / "evidence" / ".env").write_text("SECRET=private")
    bundle = tmp_path / "handoff.json"
    export_work(source, work.name, bundle, ["evidence/check.txt"])
    content = bundle.read_text()
    assert "SECRET" not in content and '"profiles"' not in content
    assert set(json.loads(content)["payload"]["evidence"]) == {"evidence/check.txt"}
    with pytest.raises(ValidationError, match="Secret"):
        export_work(source, work.name, tmp_path / "bad.json", ["evidence/.env"])
