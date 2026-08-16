"""Session-wide test isolation.

Most tests build their own ``MemoryStore(":memory:")`` and monkeypatch ``get_store``
in the module under test, which is the right pattern and stays the norm. But a few
paths reach the *process-wide* store singleton without going through a fixture —
``app.pipeline`` touching the facts a turn used, the signal writer flushing its
queue, the maintenance loop — and ``memory_db_path`` defaults to ``amber.db`` in the
working directory. That means running the suite migrated and wrote to the developer's
real database.

Pointing the whole session at a throwaway file makes that impossible rather than
merely unlikely. It has to be set before ``app.config`` is first imported, since
settings are cached with ``lru_cache`` and the module-level ``settings`` handle is
built at import time.
"""

import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="amber-tests-")
os.environ["AMBER_MEMORY_DB_PATH"] = os.path.join(_tmp, "test-amber.db")
# Background loops have no business running during tests: the maintenance pass would
# otherwise fire on every TestClient app startup.
os.environ.setdefault("AMBER_FEATURE_MAINTENANCE", "false")


@pytest.fixture(autouse=True)
def _isolate_peer_registry():
    """Empty the process-wide peer registry around every test.

    `app.peers.registry()` returns `agent_mcp`'s module-level singleton on purpose —
    a second registry would be a second answer to "where is bloom", and
    `agent_runtime` reaches for that same object when given no resolver. The price of
    that correctness is shared mutable state, and it now has more than one writer:
    anything that calls `build_broker` or `build_ecosystem_block` re-asserts the
    static layer as a side effect.

    That caught us immediately. A new test in `test_ecosystem.py` built a broker with
    a peer configured, left it in the registry, and `test_peers.py` — which runs
    later and had only a *snapshot-and-restore* fixture of its own — inherited it as
    its baseline. One test failed, in the whole-suite run only, several files away
    from the cause.
    - Clearing rather than snapshotting is what makes that unrepresentable: every
      test starts from an empty registry regardless of what ran before it, so
      ordering cannot change an outcome.
    - In conftest rather than in one file, because the state is process-wide and any
      test file can now write to it.
    """
    from agent_mcp.registry import default_registry

    reg = default_registry()
    reg._static, reg._discovered = {}, {}
    yield reg
    reg._static, reg._discovered = {}, {}
