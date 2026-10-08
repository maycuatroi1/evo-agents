"""The budget of a run the night shift queued, in the adapters: Claude Code gets ``max_budget_usd`` and ``max_turns``
and stops at them, and every adapter stops its agent at the time cap, the cap that stops a Codex run, since Codex
reports no cost. Without Postgres, over the fakes of ``tests.worker.test_runtimes``. How the daemon hands each agent of
a plan run what its session spent, and names the cap in the run's log, is in ``tests.worker.test_plan_runs``."""

from __future__ import annotations

import asyncio
import dataclasses

from evo_agents.hub import curator
from tests.worker.test_runtimes import (
    INIT,
    FakeCLI,
    UnderTest,
    _codex,
    _codex_samples,
    _collect,
    _context,
    _result,
    _samples,
    codex_turn_samples,
    needs,
)

BUDGET = {"max_usd": 0.5, "max_turns": 40, "max_seconds": 1800, "spent_usd": 0.0, "spent_seconds": 0}


def claude(tmp_path, behaviour, *, spent_usd: float = 0.0, spent_seconds: float = 0.0, **run) -> UnderTest:
    needs("claude_agent_sdk")
    cls = type("Claude", (UnderTest,), {"behaviour": staticmethod(behaviour)})
    context = _context(tmp_path, "claude-code", **run)
    return cls(dataclasses.replace(context, spent_usd=spent_usd, spent_seconds=spent_seconds))


def command_of(adapter) -> list[str]:
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    return SubprocessCLITransport(prompt="", options=adapter.options)._build_command()


def flag(command: list[str], name: str) -> str | None:
    return command[command.index(name) + 1] if name in command else None


# The model of a budget


def test_a_budget_is_read_from_the_run_and_what_is_left_never_falls_below_a_cent():
    assert curator.budget_of({"budget": None}) is None and curator.budget_of({}) is None
    assert curator.budget_of({"budget": {"spent_usd": 1}}) is None, "a budget names a cap"
    found = curator.budget_of({"budget": {**BUDGET, "max_turns": True}})
    assert found["max_usd"] == 0.5 and found["max_turns"] is None, "a boolean is no number"
    assert curator.usd_left(BUDGET, 0.2) == 0.3
    assert curator.usd_left(BUDGET, 0.7) == curator.MIN_RUN_USD
    assert curator.usd_left({"max_usd": None}, 0.0) is None
    assert curator.seconds_left(BUDGET, 1700.5) == 99.5 and curator.seconds_left(BUDGET, 4000) == 0.0


# Claude Code


def test_claude_code_gets_the_runs_budget_less_what_its_session_spent_and_its_turns(tmp_path):
    adapter = claude(tmp_path, _samples, budget=BUDGET, spent_usd=0.2)
    adapter.options = adapter.build_options("/opt/bin/claude")
    assert (adapter.options.max_budget_usd, adapter.options.max_turns) == (0.3, 40)
    command = command_of(adapter)
    assert (flag(command, "--max-budget-usd"), flag(command, "--max-turns")) == ("0.3", "40")

    spent = claude(tmp_path, _samples, budget=BUDGET, spent_usd=0.9)
    assert spent.build_options("/opt/bin/claude").max_budget_usd == curator.MIN_RUN_USD

    plain = claude(tmp_path, _samples)  # a run dispatched by hand has no budget
    plain.options = plain.build_options("/opt/bin/claude")
    assert (plain.options.max_budget_usd, plain.options.max_turns) == (None, None)
    assert "--max-budget-usd" not in command_of(plain) and "--max-turns" not in command_of(plain)


def _stopped_by(subtype: str, error: str):
    async def behaviour(cli: FakeCLI) -> None:
        await cli.users.get()
        cli.print(INIT)
        stopped = _result("", subtype=subtype, is_error=True)
        stopped.update(errors=[error], total_cost_usd=0.5023, result=None)
        cli.print(stopped)
        await cli.input_closed.wait()
        cli.exit()

    return behaviour


def test_claude_code_stopping_at_its_budget_ends_the_run_with_the_cost_cap(tmp_path):
    behaviour = _stopped_by("error_max_budget_usd", "Reached maximum budget ($0.5)")
    events, outcome = asyncio.run(_collect(claude(tmp_path, behaviour, budget=BUDGET)))
    assert not outcome.completed and outcome.cap == "cost"
    assert outcome.error == (
        "claude-code stopped at the run's cost cap of $0.50: the session cost $0.50 (Reached maximum budget ($0.5))"
    )
    assert outcome.usage["total_cost_usd"] == 0.5023
    assert events[-1].kind == "usage_update", "the cost reaches the run's log"


def test_claude_code_stopping_at_its_turns_names_the_turn_cap(tmp_path):
    behaviour = _stopped_by("error_max_turns", "Reached maximum number of turns (40)")
    _, outcome = asyncio.run(_collect(claude(tmp_path, behaviour, budget=BUDGET)))
    assert (outcome.completed, outcome.cap) == (False, "turns")
    assert outcome.error == "claude-code stopped at the run's cap of 40 turns (Reached maximum number of turns (40))"


# The time cap, which stops Codex


async def _never_ends(codex, turn) -> None:
    turn.send(*codex_turn_samples(turn.id)[1])  # a command starts, and the turn goes on until interrupted


def test_codex_stops_at_the_runs_time_cap(tmp_path):
    adapter = _codex(tmp_path, _never_ends, budget={**BUDGET, "max_usd": None, "max_seconds": 0.3})
    _, outcome = asyncio.run(_collect(adapter))
    assert ("interrupt", "turn-1") in adapter.fake.log
    assert (outcome.completed, outcome.cap) == (False, "time")
    assert outcome.error == "codex stopped at the run's time cap of 0.005 minutes of agent time"
    assert adapter.fake.closed


def test_the_time_cap_counts_the_agent_time_the_run_spent_before(tmp_path):
    needs("openai_codex")
    budget = {**BUDGET, "max_usd": None, "max_seconds": 600}
    adapter = _codex(tmp_path, _never_ends, budget=budget)
    adapter.context = dataclasses.replace(adapter.context, spent_seconds=599.8)  # 0.2 s of it left
    _, outcome = asyncio.run(asyncio.wait_for(_collect(adapter), 10))
    assert outcome.cap == "time" and "time cap of 10 minutes" in outcome.error


def test_a_run_without_a_budget_has_no_time_cap_of_its_own(tmp_path):
    adapter = _codex(tmp_path, _codex_samples)
    assert adapter.budget is None
    _, outcome = asyncio.run(_collect(adapter))
    assert outcome.completed and outcome.cap is None
