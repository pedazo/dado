from __future__ import annotations

import os

import pytest


@pytest.fixture
def symlink_capable(tmp_path):
    """Skip only tests that need creating symlinks when the host cannot."""
    target = tmp_path / "symlink-capability-target"
    link = tmp_path / "symlink-capability-link"
    target.touch()
    try:
        os.symlink(target, link)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    link.unlink()
    target.unlink()
