"""Non-destructive project-local OpenCode installation."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .errors import ConflictError
from .fs import atomic_write


FILES = Path(__file__).parent / "templates" / "opencode"


def _load_manifest(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConflictError(f"Cannot read DADO managed-files manifest {path}: {exc}") from exc
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise ConflictError(f"Invalid DADO managed-files manifest: {path}")
    return value


def install(project: Path, force: bool = False) -> tuple[list[str], list[str]]:
    opencode = project / ".opencode"
    manifest_path = project / ".dado" / "managed-files.json"
    previous = _load_manifest(manifest_path)
    manifest = dict(previous)
    installed: list[str] = []
    conflicts: list[str] = []
    for source in FILES.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(FILES)
        target = opencode / relative
        key = relative.as_posix()
        content = source.read_bytes()
        new_hash = hashlib.sha256(content).hexdigest()
        if target.exists():
            old_hash = previous.get(key)
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            if old_hash is None:
                conflicts.append(f"{target}: pre-existing user-owned file preserved")
                continue
            if current_hash != old_hash and current_hash != new_hash and not force:
                conflicts.append(f"{target}: modified managed file preserved; compare with DADO template")
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(target, content.decode("utf-8"))
        manifest[key] = new_hash
        installed.append(str(target.relative_to(project)))
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return installed, conflicts


def uninstall(project: Path, remove_config: bool = False, remove_data: bool = False) -> tuple[list[str], list[str]]:
    manifest_path = project / ".dado" / "managed-files.json"
    if not manifest_path.exists():
        if remove_config:
            (project / ".dado" / "config.yaml").unlink(missing_ok=True)
        if remove_data:
            data = project / ".dado" / "work"
            if data.exists():
                shutil.rmtree(data)
        return [], []
    manifest = _load_manifest(manifest_path)
    removed, preserved = [], []
    managed_paths = {item.relative_to(FILES).as_posix() for item in FILES.rglob("*") if item.is_file()}
    for key, expected_hash in manifest.items():
        if not isinstance(key, str) or key not in managed_paths or Path(key).is_absolute() or ".." in Path(key).parts:
            preserved.append(f"invalid manifest path: {key!r}")
            continue
        path = project / ".opencode" / key
        if not path.resolve().is_relative_to((project / ".opencode").resolve()):
            preserved.append(f"invalid manifest path: {key!r}")
            continue
        if not path.is_file():
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected_hash:
            preserved.append(str(path.relative_to(project)))
            continue
        path.unlink()
        removed.append(str(path.relative_to(project)))
        parent = path.parent
        while parent != project / ".opencode":
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    manifest_path.unlink(missing_ok=True)
    if remove_config:
        (project / ".dado" / "config.yaml").unlink(missing_ok=True)
    if remove_data:
        data = project / ".dado" / "work"
        if data.exists():
            shutil.rmtree(data)
    return removed, preserved
