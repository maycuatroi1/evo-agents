"""The hold of a merge check that its own machinery failed (``evo_agents.hub.server.merge_holds``), against Postgres and
the fake GitHub of ``tests.hub.fake_github``, on the world of ``tests.hub.test_github_merge_check``.

A verdict that fails for a reason of the hub (``evo_agents.hub.merge_infra``) leaves the check judging and the plan
run's wait open, and opens a thread in the project's triage chat whose agent is asked for the run's owner; the agent
judges the check again, whose verdict answers the wait, or hands the failure back. Judge runs that end without a verdict
are held as well, and a hold nobody ends is judged again by the hub. A failure of the pull request, and one while the
project's triage is off, go back to the run as before. The projects and people are fictional.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import judge, merge_infra, tables
from evo_agents.hub.server import merge_holds
from tests.hub.live import sql
from tests.hub.test_chat_runs import thread
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture of world)
from tests.hub.test_github_merge_check import (  # noqa: F401 (world is a fixture)
    HEAD,
    VERIFY,
    ask,
    beat,
    checks_pass,
    decision,
    gate,
    judge_run,
    verdict,
    world,
)
from tests.hub.test_runs import claim, moved

FAILED_ON_BOTH = [{"command": VERIFY, "exit_code": 1, "duration_ms": 900, "base_exit_code": 1}]


def check(world):
    m = tables.merge_checks
    ((state, reason, stored, judge_run_id),) = sql(
        world.db, select(m.c.state, m.c.reason, m.c.verdict, m.c.judge_run_id)
    )
    return state, reason, stored or {}, judge_run_id


def held(world) -> dict:
    """The hold of the one check, asserted held."""
    state, _, stored, _ = check(world)
    assert state == "judging" and stored.get("triage", {}).get("held_at"), stored
    return stored["triage"]


def said(world, hold: dict) -> list[str]:
    return [item["body"] for item in thread(world.client, world.headers["owner"], hold["chat_id"], hold["root_id"])]


def agent_of(world, hold: dict) -> int:
    """Claim and start the chat run the thread's mention made for the owner; its id. The judge run that failed the
    check held mac-mini's last slot when the hub held it, so the hub asks again on a later pass, once that slot is
    free."""
    agent_run = hold["agent_run_id"]
    if agent_run is None:
        m = tables.merge_checks
        _, _, stored, _ = check(world)
        earlier = (datetime.now(UTC) - merge_holds.ASK_EVERY).isoformat()
        sql(world.db, update(m).values(verdict={**stored, "triage": {**stored["triage"], "asked_at": earlier}}))
        beat(world, free_slots=1)  # the judge run ended: mac-mini says the slot is free
        assert checks_pass(world) == {"checks_held": 1}
        agent_run = held(world)["agent_run_id"]
        assert agent_run is not None, said(world, hold)
        assert said(world, hold)[-1].startswith("No chat run of the agent took this merge check yet: asking again")
    for _ in range(4):
        spec = claim(world.client, world.worker)
        if spec and spec["id"] == agent_run:
            moved(world.client, world.worker, agent_run, "running")
            return agent_run
    raise AssertionError(f"chat run {agent_run} was not claimed")


def triaged(world, agent_run: int, action: str, reason: str = "the judge's worker runs git 2.43 now"):
    body = {"action": action, "reason": reason}
    return world.client.post(
        f"/v1/worker/runs/{agent_run}/triage/merge-check", json=body, headers=world.worker["headers"]
    )


def judged_failing(world, **extra) -> tuple[dict, dict]:
    """Ask for the check of pull request #7 at HEAD and have its Judge post a verdict with ``extra``; (the check
    asked, the verdict's answer)."""
    found = ask(world).json()
    assert checks_pass(world) == {"checks_judging": 1}
    spec, headers = judge_run(world)
    answered = verdict(world, spec, headers, HEAD, **extra)
    assert answered.status_code == 200, answered.text
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    return found, answered.json()


def test_merge_check_a_check_the_base_fails_too_is_held_for_triage_whose_agent_judges_it_again(world):
    found, answered = judged_failing(world, verify=FAILED_ON_BOTH)
    assert (answered["passed"], answered["state"]) == (False, "judging")
    assert "held for the hub's triage" in answered["reason"] and "fails on the base branch alone" in answered["reason"]
    assert decision(world, found["decision_id"]).state == "open", "the run stays parked: no failure reaches it"
    hold = held(world)
    assert (hold["causes"], hold["rounds"]) == ([merge_infra.BASE_FAILS], 1)
    (first, *_) = said(world, hold)
    assert "pull request #7 of evo-agents" in first and "not of the pull request" in first
    assert f"waits for the verdict on its wait #{found['decision_id']}" in first
    assert "worker triage merge-check rejudge" in first and "asks owner, its owner, nothing" in first
    assert "@" in first.splitlines()[-1], "the thread mentions the project's agent"
    asked = ask(world).json()
    assert (asked["state"], asked["decision_id"]) == ("judging", found["decision_id"]), "asking again keeps the wait"
    blocked = gate(world)
    assert not blocked["passed"] and "its triage fixes a failure of the hub's own" in blocked["reason"]
    assert checks_pass(world) == {"checks_held": 1}, "no judge run while the triage holds it"

    agent_run = agent_of(world, hold)
    done = triaged(world, agent_run, "rejudge")
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "judging"
    state, reason, stored, _ = check(world)
    assert "first" not in stored and reason.startswith("judged again: its triage asked")
    assert said(world, hold)[-1].startswith("Decision: judge the merge check of pull request #7")
    audit = tables.audit
    (target,) = [row[0] for row in sql(world.db, select(audit.c.target).where(audit.c.action == "merge.triage"))]
    assert "rejudge" in target and f"chat_run:{agent_run}" in target
    assert triaged(world, agent_run, "rejudge").status_code == 409, "the check is no longer held"
    moved(world.client, world.worker, agent_run, "verifying", "done")  # the agent ends: mac-mini's slot frees

    assert checks_pass(world) == {"checks_judging": 1}
    spec, headers = judge_run(world)
    assert verdict(world, spec, headers, HEAD).status_code == 200
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    assert checks_pass(world) == {"checks_passed": 1}
    woke = decision(world, found["decision_id"])
    assert woke.state == "answered" and f"gh pr merge 7 --match-head-commit {HEAD}" in woke.answer_text
    assert "passed" in said(world, hold)[-1], "the thread hears how it ended"


def test_merge_check_a_failure_of_the_pull_request_goes_back_to_the_run_unheld(world):
    found, answered = judged_failing(world, verify=[{**FAILED_ON_BOTH[0], "base_exit_code": 0}])
    assert (answered["passed"], answered["state"]) == (False, "failed")
    _, _, stored, _ = check(world)
    assert not stored.get("triage")
    woke = decision(world, found["decision_id"])
    assert woke.state == "answered" and "fix it, push, and ask again" in woke.answer_text


def test_merge_check_the_triage_agent_hands_a_failure_back_to_the_run(world):
    found, answered = judged_failing(world, security=None)
    assert answered["state"] == "judging"
    hold = held(world)
    assert hold["causes"] == [merge_infra.NO_SECURITY]
    done = triaged(world, agent_of(world, hold), "return", "the diff prints a token: the change's to fix")
    assert done.status_code == 200 and done.json()["state"] == "failed", done.text
    state, reason, _, _ = check(world)
    assert state == "failed" and reason.endswith(
        "its triage handed it back: the diff prints a token: the change's to fix"
    )
    woke = decision(world, found["decision_id"])
    assert woke.state == "answered" and "fix it, push, and ask again" in woke.answer_text
    assert ask(world).json()["state"] == "failed", "the pull request's failure: asking again does not judge it again"


def test_merge_check_judge_runs_ending_without_a_verdict_are_held_and_judged_again_after_the_hold(world):
    found = ask(world).json()
    for _ in range(judge.JUDGE_ATTEMPTS):
        assert checks_pass(world) == {"checks_judging": 1}
        spec, _ = judge_run(world)
        moved(world.client, world.worker, spec["id"], "failed", failure_cause="missing_tool", error="git: merge-tree")
    assert checks_pass(world) == {"checks_held": 1}
    hold = held(world)
    assert hold["causes"] == [merge_infra.JUDGE_SETUP]
    assert f"judge run #{spec['id']} ended failed (missing_tool) without a verdict: git: merge-tree" in hold["reason"]
    assert f"/runs/{spec['id']}" in said(world, hold)[0]
    assert decision(world, found["decision_id"]).state == "open"

    m = tables.merge_checks
    _, _, stored, _ = check(world)
    old = (datetime.now(UTC) - timedelta(hours=merge_infra.HOLD_HOURS + 1)).isoformat()
    sql(world.db, update(m).values(verdict={**stored, "triage": {**stored["triage"], "held_at": old}}))
    assert checks_pass(world) == {"checks_released": 1, "checks_judging": 1}
    state, reason, stored, judge_run_id = check(world)
    assert judge_run_id is not None and stored["triage"]["rounds"] == 1 and not stored["triage"].get("held_at")
    assert "judges pull request #7 of evo-agents again by itself" in said(world, hold)[-1]


def test_merge_check_a_failure_of_the_hub_with_triage_off_goes_back_saying_so_and_asking_again_judges_it(world):
    sql(world.db, update(tables.projects).values(triage_enabled=False))
    found, answered = judged_failing(world, verify=FAILED_ON_BOTH)
    assert answered["state"] == "failed"
    woke = decision(world, found["decision_id"])
    assert woke.state == "answered" and "The hub's own machinery failed it" in woke.answer_text
    assert "do not ask the owner" in woke.answer_text and "fix it, push" not in woke.answer_text
    again = ask(world).json()
    assert again["state"] == "judging" and again["decision_id"] != found["decision_id"], "judged again, on a new wait"
    _, _, stored, _ = check(world)
    assert "first" not in stored
