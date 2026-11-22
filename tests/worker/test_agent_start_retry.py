"""An agent that does not start is started once more before its run fails (``Agent.started``): a loaded machine fails a
start now and then (``Control request timeout: initialize``), and seven chat runs of one worker failed so on
2026-11-15 and 16 with no try again. A refused profile fails at once; the last failure says the cause ``runtime``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker.adapter import ProfileRefused  # noqa: E402
from evo_agents.worker.runner import agent as agent_module  # noqa: E402
from evo_agents.worker.runner.agent import Agent  # noqa: E402
from evo_agents.worker.runner.common import RunFailed  # noqa: E402


def run() -> SimpleNamespace:
    notes = []
    return SimpleNamespace(
        id=7,
        runtime="claude-code",
        daemon=SimpleNamespace(hard_stop=asyncio.Event()),
        note=lambda text, **extra: notes.append((text, extra)),
        notes=notes,
    )


def adapter(failures: list[BaseException]):
    """An adapter class whose start raises each of ``failures`` in turn, then starts."""
    made = []

    class Fake:
        def __init__(self, context):
            made.append(self)

        async def start(self):
            if failures:
                raise failures.pop(0)

    Fake.made = made
    return Fake


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(agent_module, "START_PAUSE", 0.0)


def test_an_agent_that_did_not_start_is_started_once_more():
    found = run()
    cls = adapter([Exception("Control request timeout: initialize")])
    agent = Agent(found)
    started = asyncio.run(agent.started(cls, None))
    assert started is cls.made[1] and agent.adapter is started and len(cls.made) == 2
    ((text, _),) = found.notes
    assert text.startswith("claude-code did not start (Exception: Control request timeout: initialize)")


def test_the_second_failure_fails_the_run_with_the_cause_runtime():
    found = run()
    cls = adapter([Exception("Control request timeout: initialize"), Exception("again")])
    with pytest.raises(RunFailed) as failed:
        asyncio.run(Agent(found).started(cls, None))
    assert failed.value.error == "claude-code did not start: Exception: again"
    assert failed.value.fields == {"cause": "runtime"} and len(cls.made) == 2


def test_a_refused_profile_fails_at_once():
    found = run()
    cls = adapter([ProfileRefused("no such tool")])
    with pytest.raises(RunFailed) as failed:
        asyncio.run(Agent(found).started(cls, None))
    assert failed.value.fields == {"cause": "runtime"} and len(cls.made) == 1
    assert found.notes == [("Agent profile: no such tool; the agent did not start.", {"cause": "runtime"})]
