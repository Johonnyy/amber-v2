"""Sharing the keyword table with the ecosystem, against a fake sync store.

The store is faked at the HTTP layer rather than mocked at the function layer, so
these exercise the real request shapes `amber-infra/sync-store` serves — the two
sides of that contract are in different repos and nothing else holds them together.

What is really being asserted throughout: **local always wins until it is pushed**.
Amber must keep working, and keep obeying her own table, with the store down; sync is
reconciliation afterwards, never a dependency in front.
"""

import httpx
import pytest

from app import model_sync, models
from app.config import Settings
from app.memory.store import MemoryStore

STORE_URL = "https://sync.example/"


def _settings(**over):
    base = dict(
        memory_db_path=":memory:",
        llm_tier="balanced",
        mcp_sync_store_url=STORE_URL,
        mcp_sync_store_token="s3cret",
        feature_model_sync=True,
    )
    base.update(over)
    return Settings(_env_file=None, **base)


class FakeStore:
    """The sync store's ``/models`` half, in memory, over a transport."""

    def __init__(self, keywords: dict | None = None, *, fail: bool = False) -> None:
        self.keywords = dict(keywords or {})
        self.fail = fail
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.fail:
            return httpx.Response(503, json={"error": "unavailable", "message": "down"})
        # Every call must be authenticated — a store that 401s is the normal
        # consequence of a missing token, and silently unauthenticated writes here
        # would only show up as a shared table nobody could write.
        assert request.headers.get("authorization") == "Bearer s3cret"

        name = request.url.path.removeprefix("/models").strip("/")
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "keywords": self.keywords,
                    "count": len(self.keywords),
                    "generated_at": "2026-01-01T00:00:00+00:00",
                },
            )
        if request.method == "PUT" and name:
            import json as _json

            body = _json.loads(request.content)
            self.keywords[name] = {
                "model": body["model"],
                "description": body.get("description", ""),
                "updated_at": "2026-01-01T00:00:00+00:00",
                "updated_by": "amber",
            }
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "DELETE" and name:
            if name not in self.keywords:
                return httpx.Response(404, json={"error": "not_found", "message": name})
            del self.keywords[name]
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(405, json={"error": "error", "message": request.method})


@pytest.fixture
def local(monkeypatch):
    """Amber's own table, isolated."""
    store = MemoryStore(":memory:")
    import app.memory as memory_pkg

    monkeypatch.setattr(memory_pkg, "get_store", lambda: store)
    models.invalidate_cache()
    yield store
    models.invalidate_cache()
    store.close()


@pytest.fixture
def remote(monkeypatch):
    """Point `httpx.AsyncClient` at a fake store for the duration of a test."""
    fake = FakeStore()
    real_client = httpx.AsyncClient

    def build(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(model_sync.httpx, "AsyncClient", build)
    return fake


# --- the switch ---


def test_sync_is_off_without_a_store_url():
    """The ecosystem's founding rule: an app runs standalone, knowing nothing."""
    assert model_sync.sync_enabled(_settings(mcp_sync_store_url="")) is False
    assert model_sync.sync_enabled(_settings(feature_model_sync=False)) is False
    assert model_sync.sync_enabled(_settings()) is True


async def test_a_disabled_sync_touches_nothing(local, remote):
    report = await model_sync.sync_once(_settings(feature_model_sync=False))
    assert report["ok"] is False
    assert remote.requests == []


# --- push ---


async def test_a_local_override_is_pushed(local, remote):
    models.set_keyword("coding", "vendor/coder-9", description="Code.")
    report = await model_sync.sync_once(_settings())

    assert report["pushed"] == 1
    assert remote.keywords["coding"]["model"] == "vendor/coder-9"
    assert remote.keywords["coding"]["description"] == "Code."
    # And the row is now known to be shared, so it is not pushed again.
    assert local.model_keywords()["coding"]["synced"] is True
    assert (await model_sync.sync_once(_settings()))["pushed"] == 0


async def test_a_local_reset_removes_it_from_the_shared_table(local, remote):
    models.set_keyword("coding", "vendor/coder-9")
    await model_sync.sync_once(_settings())
    models.reset_keyword("coding")

    report = await model_sync.sync_once(_settings())
    assert report["removed"] == 1
    assert remote.keywords == {}
    assert local.model_keywords() == {}  # the tombstone is gone once it lands


async def test_a_reset_survives_an_unreachable_store(local, remote):
    """The tombstone's whole reason to exist: without it the next pull would bring
    the override straight back and the reset would look like it never happened."""
    models.set_keyword("coding", "vendor/coder-9")
    await model_sync.sync_once(_settings())

    remote.fail = True
    models.reset_keyword("coding")
    await model_sync.sync_once(_settings())
    # Locally it is already reset, whatever the store thinks.
    assert models.resolve("coding", _settings()) == models.BUILTIN_MODELS["coding"]

    remote.fail = False
    await model_sync.sync_once(_settings())
    assert remote.keywords == {}
    assert models.resolve("coding", _settings()) == models.BUILTIN_MODELS["coding"]


async def test_a_deletion_of_something_already_gone_is_not_an_error(local, remote):
    models.set_keyword("coding", "vendor/coder-9")
    await model_sync.sync_once(_settings())
    remote.keywords.clear()  # removed from another device in the meantime
    models.reset_keyword("coding")

    report = await model_sync.sync_once(_settings())
    assert report["ok"] is True
    assert local.model_keywords() == {}


# --- pull ---


async def test_another_apps_keyword_arrives_with_its_description(local, remote):
    """A keyword invented elsewhere is a bare word here without the description."""
    remote.keywords["sql"] = {
        "model": "vendor/db-tuned",
        "description": "Schema and query work.",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_by": "finance",
    }
    report = await model_sync.sync_once(_settings())

    assert report["pulled"] == 1
    assert models.resolve("sql", _settings()) == "vendor/db-tuned"
    entry = [k for k in models.options(_settings())["keywords"] if k["name"] == "sql"][0]
    assert entry["description"] == "Schema and query work."
    assert entry["shared"] is True


async def test_the_shared_table_re_points_a_builtin(local, remote):
    remote.keywords["coding"] = {
        "model": "vendor/coder-remote",
        "description": "",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_by": "finance",
    }
    await model_sync.sync_once(_settings())
    assert models.resolve("coding", _settings()) == "vendor/coder-remote"


async def test_a_removal_elsewhere_restores_the_local_default(local, remote):
    remote.keywords["writing"] = {
        "model": "vendor/prose",
        "description": "",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_by": "finance",
    }
    await model_sync.sync_once(_settings())
    remote.keywords.clear()

    report = await model_sync.sync_once(_settings())
    assert report["pruned"] == 1
    assert models.resolve("writing", _settings()) == models.BUILTIN_MODELS["writing"]


async def test_an_unpushed_local_change_is_not_overwritten_by_the_pull(local, remote):
    """A failed push must not be undone by the very value it was trying to replace."""
    remote.keywords["coding"] = {
        "model": "vendor/old",
        "description": "",
        "updated_at": "2026-01-01T00:00:00+00:00",
        "updated_by": "finance",
    }
    await model_sync.sync_once(_settings())

    remote.fail = True
    models.set_keyword("coding", "vendor/new")
    await model_sync.sync_once(_settings())
    assert models.resolve("coding", _settings()) == "vendor/new"

    remote.fail = False
    await model_sync.sync_once(_settings())
    assert remote.keywords["coding"]["model"] == "vendor/new"


async def test_a_malformed_shared_row_does_not_stop_the_rest(local, remote):
    """One bad row in a table every app reads must cost that row, not the table."""
    remote.keywords.update(
        {
            "coding": {"model": "not-a-model-id", "description": ""},
            "writing": {"model": "vendor/prose", "description": ""},
        }
    )
    report = await model_sync.sync_once(_settings())

    assert report["ok"] is True
    assert models.resolve("coding", _settings()) == models.BUILTIN_MODELS["coding"]
    assert models.resolve("writing", _settings()) == "vendor/prose"


# --- failure is a normal state ---


async def test_an_unreachable_store_never_raises_and_says_so(local, remote):
    remote.fail = True
    models.set_keyword("coding", "vendor/coder-9")

    report = await model_sync.sync_once(_settings())
    assert report["ok"] is False
    # The local table is untouched and still authoritative for the next turn.
    assert models.resolve("coding", _settings()) == "vendor/coder-9"

    status = model_sync.status(_settings())
    assert status["enabled"] is True
    assert status["pending"] == 1
    assert status["last_error"]
