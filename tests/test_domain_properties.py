from itertools import product

from dado.domain import invalidate_closure, ready_tasks, scope_matches, scopes_overlap


def test_disjoint_glob_prefixes_and_ambiguous_patterns():
    assert not scopes_overlap(["src/frontend/**"], ["docs/**"])
    assert not scopes_overlap(["src/frontend/**"], ["src/backend/**"])
    assert scopes_overlap(["src/**"], ["src/backend/**"])
    assert scopes_overlap(["src*"], ["src/backend/**"])
    assert scopes_overlap(["**/*.py"], ["docs/**"])


def test_overlap_never_declares_sampled_common_match_disjoint():
    patterns = ["src", "src/**", "src/*.py", "src/a/**", "docs/**", "**/*.py", "src*", "src/[ab]/**", "."]
    paths = ["src/a.py", "src/a/b.py", "docs/a.py", "src/b/a.py", "src2/a.py"]
    for a, b in product(patterns, repeat=2):
        if any(scope_matches(path, a) and scope_matches(path, b) for path in paths):
            assert scopes_overlap([a], [b]), (a, b)


def test_long_chain_invalidates_all_descendants():
    tasks = [{"id": str(i), "status": "done", "depends_on": [str(i - 1)] if i else []} for i in range(1500)]
    assert len(invalidate_closure(tasks, "0")) == 1500
    assert all(task["status"] == "stale" for task in tasks)


def test_disjoint_ready_scopes_run_together():
    tasks = [{"id": str(i), "status": "pending", "depends_on": [], "scope": {"allowed": [scope]}} for i, scope in enumerate(["src/**", "docs/**"])]
    assert len(ready_tasks(tasks)) == 2
