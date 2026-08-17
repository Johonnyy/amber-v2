"""Replaying the turns that went wrong.

CLAUDE.md has asked for "small per-app eval sets (10-20 hand-written query →
expected-tool-call cases)" since the beginning, and not one has ever been written. The
reason is mundane and worth naming: the moment you would write a case is the moment a
turn goes wrong, and at that moment you are looking at a conversation, not a test file.
So the case is captured from the timeline (`app.review.capture_eval`) and this is the
half that replays it.

**What a case asserts is which tool got called, not what was said.** Comparing replies
would need a judge and would fail on rewording; the tool choice is the decision that
actually goes wrong — she searched the web when she should have handed the job to Bloom,
or answered from memory when she should have looked something up. That is a discrete,
checkable fact, and it is the one CLAUDE.md asked for.

**Nothing here writes to the repository.** Cases live in `amber.db`; `export` prints
JSONL to stdout. A person can redirect that into `evals/cases.jsonl` and commit it on
their own machine, which is the only place a commit belongs — a deploy changing tracked
files is how a production box ends up with a diff nobody wrote.

    python -m app.evals            # replay every active case
    python -m app.evals export     # print them as JSONL
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from dataclasses import dataclass, field

from app.brain import build_broker
from app.config import Settings, get_settings
from app.memory.store import MemoryStore

logger = logging.getLogger(__name__)


@dataclass
class CaseResult:
    """One replayed case."""

    id: int
    query: str
    expected: str | None
    called: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        # No expectation recorded means the case only asserts "this didn't blow up",
        # which is worth keeping — plenty of bad turns are bad without a right answer
        # anyone can name.
        if self.error is not None:
            return False
        if not self.expected:
            return True
        return self.expected in self.called

    def line(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        want = self.expected or "(no expectation)"
        got = ", ".join(self.called) or "no tools"
        return f"  {mark} #{self.id} {self.query[:56]!r}\n        want {want} · got {got}"


class _RecordingBroker:
    """Wraps the real broker and records which tools the model reached for.

    Wrapping rather than faking, so a case exercises the tool list Amber would really
    offer — a case that passed against a hand-written tool list would prove nothing
    about the install it ran on.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.called: list[str] = []

    def bind(self, **kwargs) -> None:
        bind = getattr(self._inner, "bind", None)
        if bind is not None:
            bind(**kwargs)

    async def list_tools(self):
        return await self._inner.list_tools()

    async def call_tool(self, name: str, args: dict) -> str:
        self.called.append(name)
        # Deliberately not executed. A replay must not send an email, spend money, or
        # restart the server — and the assertion is about the *choice*, which has
        # already been made by the time this is reached.
        return f"(eval: {name} not executed)"

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()


async def run_case(case: dict, settings: Settings) -> CaseResult:
    """Replay one case. Never raises."""
    result = CaseResult(
        id=case["id"], query=case["query"], expected=case.get("expect_tool")
    )
    inner = build_broker(settings)
    if inner is None:
        # No tools offered at all — an install with `AMBER_FEATURE_TOOLS` off. That is a
        # configuration state, not a failing case, and reporting every case as a failure
        # would bury the one line that explains why.
        result.error = "no tools are enabled on this install"
        return result

    broker = _RecordingBroker(inner)
    try:
        from agent_runtime import AgentRunner, RunState

        from app.brain import runtime_settings
        from app.models import resolve as resolve_model
        from app.persona import SYSTEM_PROMPT

        runner = AgentRunner(
            model=resolve_model(settings.llm_tier, settings),
            broker=broker,
            settings=runtime_settings(settings),
        )
        state = RunState()
        async for _ in runner.stream(
            [{"role": "user", "content": case["query"]}],
            system=SYSTEM_PROMPT,
            state=state,
        ):
            pass
        result.called = broker.called
    except Exception as exc:  # noqa: BLE001 — one bad case must not stop the run
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        # A broker per case means an MCP session per case; a hundred-case run would
        # otherwise hold a hundred open connections to every peer.
        with contextlib.suppress(Exception):
            await broker.aclose()
    return result


async def run_all(settings: Settings | None = None, *, store=None) -> list[CaseResult]:
    settings = settings or get_settings()
    store = store or MemoryStore(settings.memory_db_path)
    cases = store.eval_cases(limit=500)
    return [await run_case(case, settings) for case in cases]


def _export(store) -> int:
    for case in store.eval_cases(limit=1000):
        print(json.dumps(case, ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay Amber's saved eval cases.")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "export"),
        help="run the cases (default), or print them as JSONL for committing by hand",
    )
    args = parser.parse_args()

    settings = get_settings()
    logging.basicConfig(level=settings.log_level.upper())
    # Built directly rather than through `get_store()`, like `app.maintenance.main`
    # does, so a manual run can't pick up a cached singleton on a different database.
    store = MemoryStore(settings.memory_db_path)
    try:
        if args.command == "export":
            sys.exit(_export(store))

        results = asyncio.run(run_all(settings, store=store))
        if not results:
            print("No eval cases saved yet — save one from a turn that went wrong.")
            return
        failed = [r for r in results if not r.passed]
        for result in results:
            print(result.line())
        print(f"\n{len(results) - len(failed)}/{len(results)} passed")
        sys.exit(1 if failed else 0)
    finally:
        store.close()


if __name__ == "__main__":
    main()
