"""Tests for the task tools — they mutate the store's `tasks` table."""

import pytest

import app.tools.tasks as tasks
from app.memory.store import MemoryStore


@pytest.fixture
def store(monkeypatch):
    s = MemoryStore(":memory:")
    monkeypatch.setattr(tasks, "get_store", lambda: s)
    yield s
    s.close()


async def test_add_task_persists_and_acks(store):
    msg = await tasks.add_task("buy milk")
    assert "buy milk" in msg
    assert [t["description"] for t in store.open_tasks()] == ["buy milk"]


async def test_add_task_rejects_blank(store):
    msg = await tasks.add_task("   ")
    assert "Error" in msg
    assert store.open_tasks() == []


async def test_list_tasks_empty_and_populated(store):
    assert "no open tasks" in (await tasks.list_tasks()).lower()
    store.add_task("call dentist")
    listed = await tasks.list_tasks()
    assert "call dentist" in listed


async def test_complete_task_marks_done(store):
    tid = store.add_task("water plants")
    msg = await tasks.complete_task(tid)
    assert "done" in msg.lower()
    assert store.open_tasks() == []


async def test_complete_unknown_task_reports_no_change(store):
    msg = await tasks.complete_task(999)
    assert "No open task" in msg
