"""Check that the built wheel includes runtime resources and its CLI entry point."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import zipfile


def main() -> None:
    wheels = sorted(Path("dist").glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise SystemExit("Build a wheel first: python -m build")
    with zipfile.ZipFile(wheels[-1]) as wheel:
        names = set(wheel.namelist())
        resources = [
            path.relative_to("src").as_posix()
            for directory in ("src/dado/schemas", "src/dado/templates")
            for path in Path(directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        ]
        missing = set(resources) - names
        if missing:
            raise SystemExit(f"Missing packaged resources: {sorted(missing)}")
        entry = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        if "dado = dado.cli:main" not in wheel.read(entry).decode():
            raise SystemExit("Missing dado CLI entry point")
    print(f"Verified {len(resources)} resources and CLI in {wheels[-1]}")
    with tempfile.TemporaryDirectory(prefix="dado-wheel-smoke-") as temporary:
        root = Path(temporary)
        target = root / "installed"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target),
                        str(wheels[-1].resolve())], check=True)
        env = {**os.environ, "PYTHONPATH": str(target)}
        subprocess.run([sys.executable, "-m", "dado.cli", "--version"], cwd=root, env=env, check=True)
        subprocess.run([sys.executable, "-m", "dado.cli", "init", "--project", str(root / "project")],
                       cwd=root, env=env, check=True)
        assert (root / "project" / ".opencode" / "agents" / "dado-worker.md").is_file()
    print("Installed wheel CLI and resource initialization passed (no model calls).")


if __name__ == "__main__":
    main()
