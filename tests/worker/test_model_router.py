"""A run's model chosen by its difficulty (plan super-board-parity-2, criterion a6): the shared rules of
``evo_agents.hub.model_router``, and the worker's side (``evo_agents.worker.model_router``, ``Agent.route_model``,
the reports) against a fake ``claude`` on PATH. None of these needs a database; what the hub does with the policy, the
claim and the reports is in ``tests/hub/test_model_router.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub import model_router
from evo_agents.worker import model_router as worker_router
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.runner.agent import Agent
from evo_agents.worker.runner.reports import Reports

ROUTING = {
    "routing": {"enabled": True, "scorer": "haiku", "ladder": {"easy": "haiku", "medium": "sonnet", "hard": "opus"}}
}
CLAIMED = {"scorer": "haiku", "ladder": {"easy": "haiku", "medium": "sonnet", "hard": "opus"}}

FAKE_CLAUDE = """#!{python}
import json, os, sys, time
question = sys.stdin.read()
with open(os.environ["FAKE_SCORER_LOG"], "w") as log:
    json.dump({{"argv": sys.argv[1:], "question": question, "cwd": os.getcwd()}}, log)
mode = os.environ.get("FAKE_SCORER", "hard")
if mode == "sleep":
    time.sleep(30)
if mode == "garbage":
    print("not json")
    sys.exit(1)
if mode == "error":
    print(json.dumps({{"type": "result", "is_error": True, "result": "API Error: overloaded", "total_cost_usd": 0}}))
    sys.exit(1)
print(json.dumps({{"type": "result", "is_error": False, "result": mode, "total_cost_usd": 0.0012}}))
"""


def profile(*models: str) -> dict:
    """A profile as the claim carries it that allows ``models`` (none given: all)."""
    model = {"mode": "allowlist", "allow": list(models)} if models else {"mode": "all"}
    return {"handle": "dev", "version": 2, "name": "Dev", "instructions": "", "permissions": {"model": model}}


@pytest.fixture
def scorer_env(tmp_path, monkeypatch):
    """The agent's environment with a fake ``claude`` first on PATH; ``FAKE_SCORER`` says what it answers."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_SCORER_LOG": str(tmp_path / "log.json")}


# The shared rules


def test_model_router_claim_carries_routing_only_when_the_policy_has_it_on():
    assert model_router.claim_routing(ROUTING, "step", "claude-code", None) == CLAIMED
    assert model_router.claim_routing(ROUTING, "task", "claude-code", "") == CLAIMED
    off = {"routing": {**ROUTING["routing"], "enabled": False}}
    assert model_router.claim_routing(off, "step", "claude-code", None) is None
    assert model_router.claim_routing({}, "step", "claude-code", None) is None
    assert model_router.claim_routing(None, "step", "claude-code", None) is None


def test_model_router_skips_a_pinned_run_and_kinds_and_runtimes_it_does_not_route():
    assert model_router.claim_routing(ROUTING, "step", "claude-code", "opus") is None
    assert model_router.claim_routing(ROUTING, "judge", "claude-code", None) is None
    assert model_router.claim_routing(ROUTING, "chat", "claude-code", None) is None
    assert model_router.claim_routing(ROUTING, "step", "codex", None) is None
    assert model_router.pinned_by_profile(["opus"])
    assert not model_router.pinned_by_profile(["opus", "sonnet"])
    assert not model_router.pinned_by_profile(None)


def test_model_router_reads_the_difficulty_from_the_scorers_answer():
    assert model_router.difficulty_of("hard") == "hard"
    assert model_router.difficulty_of("**Easy**.") == "easy"
    assert model_router.difficulty_of("Rating: medium") == "medium"
    assert model_router.difficulty_of("I cannot tell") is None
    assert model_router.difficulty_of(None) is None
    assert "the task body" in model_router.scorer_prompt("the task body")
    assert len(model_router.scorer_prompt("x" * 50_000)) < 50_000


def test_model_router_record_falls_back_to_medium_when_not_scored():
    assert model_router.record("haiku", None, "sonnet", error="e" * 1000) == {
        "scorer": "haiku",
        "difficulty": "medium",
        "scored": False,
        "model": "sonnet",
        "cost_usd": None,
        "error": "e" * model_router.MAX_ERROR_CHARS,
    }
    assert model_router.record("haiku", "hard", "opus", 0.002)["scored"] is True


# The worker


def test_model_router_routing_of_skips_a_pinned_run():
    assert worker_router.routing_of({"routing": CLAIMED}) == CLAIMED
    assert worker_router.routing_of({}) is None
    assert worker_router.routing_of({"routing": CLAIMED, "model": "opus"}) is None
    assert worker_router.routing_of({"routing": CLAIMED, "profile": profile("opus")}) is None
    assert worker_router.routing_of({"routing": CLAIMED, "profile": profile("opus", "sonnet")}) == CLAIMED


def test_model_router_reads_the_json_answer_of_claude_print():
    ok = json.dumps({"result": "easy", "total_cost_usd": 0.001, "is_error": False}).encode()
    assert worker_router.read_answer(0, ok, b"") == ("easy", 0.001, None)
    failed = json.dumps({"result": "API Error", "total_cost_usd": 0, "is_error": True}).encode()
    answer, cost, error = worker_router.read_answer(1, failed, b"")
    assert (answer, cost) == (None, 0.0) and "API Error" in error
    answer, cost, error = worker_router.read_answer(127, b"", b"command not found")
    assert (answer, cost) == (None, None) and "command not found" in error


def test_model_router_rates_the_run_with_one_short_turn_of_the_scorer(scorer_env, tmp_path):
    spec = {"routing": CLAIMED, "prompt": "Rename one helper in one file."}
    found = asyncio.run(worker_router.route(spec, {**scorer_env, "FAKE_SCORER": "hard"}))
    assert found == {"scorer": "haiku", "difficulty": "hard", "scored": True, "model": "opus", "cost_usd": 0.0012,
                     "error": None}  # fmt: skip
    seen = json.loads((tmp_path / "log.json").read_text())
    assert seen["argv"][:3] == ["--print", "--model", "haiku"]
    assert {"--tools", "--strict-mcp-config", "--no-session-persistence"} <= set(seen["argv"])
    assert seen["argv"][seen["argv"].index("--max-turns") + 1] == "1"
    assert "Rename one helper in one file." in seen["question"]
    assert os.path.basename(seen["cwd"]).startswith("evo-scorer-")


@pytest.mark.parametrize("mode", ["error", "garbage", "maybe"])
def test_model_router_a_failed_rating_takes_the_medium_rung(scorer_env, mode):
    found = asyncio.run(worker_router.route({"routing": CLAIMED, "prompt": "p"}, {**scorer_env, "FAKE_SCORER": mode}))
    assert (found["difficulty"], found["scored"], found["model"]) == ("medium", False, "sonnet")
    assert found["error"]


def test_model_router_a_scorer_out_of_time_or_missing_takes_the_medium_rung(scorer_env):
    spec = {"routing": CLAIMED, "prompt": "p"}
    slow = asyncio.run(worker_router.route(spec, {**scorer_env, "FAKE_SCORER": "sleep"}, timeout=0.5))
    assert (slow["model"], slow["scored"]) == ("sonnet", False) and "within" in slow["error"]
    missing = asyncio.run(worker_router.route(spec, {"PATH": "/nonexistent"}))
    assert (missing["model"], missing["scored"]) == ("sonnet", False) and "not on PATH" in missing["error"]


def test_model_router_does_not_take_a_ladder_model_the_profile_leaves_out(scorer_env):
    spec = {"routing": CLAIMED, "prompt": "p", "profile": profile("haiku", "sonnet")}
    found = asyncio.run(worker_router.route(spec, {**scorer_env, "FAKE_SCORER": "hard"}))
    assert (found["difficulty"], found["model"]) == ("hard", None)
    assert "does not allow the ladder's model opus" in found["error"]


def test_model_router_does_not_rate_a_pinned_run(scorer_env, tmp_path):
    for spec in ({"routing": CLAIMED, "model": "opus"}, {"routing": CLAIMED, "profile": profile("opus")}, {}):
        assert asyncio.run(worker_router.route({**spec, "prompt": "p"}, scorer_env)) is None
    assert not (tmp_path / "log.json").exists()


# The agent and the reports


def _agent(spec: dict, runtime: str = "claude-code") -> Agent:
    notes: list[str] = []
    run = SimpleNamespace(id=7, spec=spec, runtime=runtime, notes=notes, note=lambda text, **_: notes.append(text))
    return Agent(run)


def test_model_router_agent_runs_on_the_ladder_model_once(scorer_env):
    agent = _agent({"routing": CLAIMED, "prompt": "p"})
    context = RunContext(
        run=agent.run.spec, worktree=Path("/tmp"), prompt="p", env={**scorer_env, "FAKE_SCORER": "easy"}
    )
    assert asyncio.run(agent.route_model(context)) is True
    assert agent.run.spec["model"] == "haiku"
    assert agent.routing["difficulty"] == "easy" and "runs on haiku" in agent.run.notes[0]
    assert asyncio.run(agent.route_model(context)) is False  # a second agent of the run is not rated again
    agent.model_asked = agent.asked_model(context)
    assert agent.model_fields() == {"model_used": "haiku", "routing": agent.routing}
    agent.adapter = SimpleNamespace(model_used="claude-haiku-4-5-20251001")
    assert agent.model_fields()["model_used"] == "claude-haiku-4-5-20251001"


def test_model_router_agent_without_routing_still_says_its_model():
    agent = _agent({"prompt": "p", "model": "opus"})
    context = RunContext(run=agent.run.spec, worktree=Path("/tmp"), prompt="p", env={})
    assert asyncio.run(agent.route_model(context)) is False
    assert agent.routing is None and not agent.run.notes
    agent.model_asked = agent.asked_model(context)
    assert agent.model_fields() == {"model_used": "opus", "routing": None}


def test_model_router_reports_carry_the_model_used_and_the_routing():
    sent: list[tuple[str, dict]] = []
    agent = SimpleNamespace(model_fields=lambda: {"model_used": "claude-opus", "routing": {"difficulty": "hard"}},
                            session_reported=None, adapter=None)  # fmt: skip
    run = SimpleNamespace(id=7, agent=agent)
    reports = Reports.__new__(Reports)
    reports.run = run

    async def report(state, **fields):
        sent.append((state, fields))
        return {}

    reports.report = report
    asyncio.run(reports.report_session("running", ("running",), "session-1"))
    assert sent[-1][1]["model_used"] == "claude-opus" and sent[-1][1]["routing"] == {"difficulty": "hard"}


def test_model_router_claude_code_records_the_model_its_init_message_names(tmp_path):
    from tests.worker.test_runtimes import _claude, _collect, _samples

    adapter = _claude(tmp_path, _samples)
    assert adapter.model_used is None
    asyncio.run(_collect(adapter))
    assert adapter.model_used == "claude-opus-5-5"
