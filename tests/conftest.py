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

_tmp = tempfile.mkdtemp(prefix="amber-tests-")
os.environ["AMBER_MEMORY_DB_PATH"] = os.path.join(_tmp, "test-amber.db")
# Background loops have no business running during tests: the maintenance pass would
# otherwise fire on every TestClient app startup.
os.environ.setdefault("AMBER_FEATURE_MAINTENANCE", "false")
