"""The Curator's ledger, the outcome of its merged changes and the circuit breaker of the night shift
(``evo_agents.hub.ledger``, ``evo_agents.hub.server.ledger``, ``evo_agents.hub.server.outcomes``); and schema 0018.

The checks step 7 of the curator-agent plan names for a11: each proposal has lines in a ledger the hub only adds to,
each naming who acted (the hub's code, an agent with its run, a member), what happened, the commit, the default
branch before and after, the figures that set the proposal off, the Judge's verdict, the pull request and when it
merged; outcome_days after a merge (7 by default) the job curator.outcomes counts the same figures again with
curator.collect's count and records keep, revert or unclear, and figures that got worse make the hub propose a revert
of tier 1, a git revert of the merge commit; two jobs of a night in a row that failed or were reverted pause the
project's night shift and tell its owner. The web shows a proposal's ledger on its page (web/e2e/curator.spec.ts).
GitHub is the fake of ``tests.hub.fake_github``, as in ``tests.hub.test_curator_changes``."""

from __future__ import annotations

import ast
from datetime import time, timedelta
from functools import partial
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, insert, select, update

from evo_agents.hub import curator, jobs, ledger, runs, tables
from evo_agents.hub.server.outcomes import measure_outcomes
from evo_agents.hub.server.proposals import _check_draft
from evo_agents.hub.worker import queue
from tests.hub.live import sql
from tests.hub.test_credentials_api import MINE, app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import (
    NIGHT,
    PROJECT,
    THE_NIGHT,
    charter_body,
    charter_path,
    ended,
    fire,
    night,  # noqa: F401 (a fixture)
    scheduled,
    status_of,
    written,
)
from tests.hub.test_curator_changes import (
    BRANCH,
    GREEN,
    HEAD,
    SUCCESS,
    accepted,
    builder,
    changes,
    curator_charter,
    judged,
    moved_on,
    one_change,
    path,
    proposal_of,
    protect,
    through_the_judge,
    world,  # noqa: F401 (a fixture)
)
from tests.hub.test_migrate import move_to
from tests.hub.test_review_runs import answered, digest_body, push_digest
from tests.hub.test_runs import OWNER, claim, control, moved, web_client  # noqa: F401 (a fixture)

HAND_MERGE = "4" * 40
SESSIONS = "0199a3c1-0000-7000-8000-{:012d}"
ROOT = Path(__file__).parents[2]


def ledger_of(world, proposal_id: int, headers=None) -> dict:  # noqa: F811
    response = world.client.get(
        path("proposals", str(proposal_id), "ledger"), headers=headers or world.headers["owner"]
    )
    assert response.status_code == 200, response.text
    return response.json()


def acts(found: dict) -> list[tuple]:
    return [(line["action"], line["actor"]) for line in found["lines"]]


def measured(world, now) -> dict:  # noqa: F811
    return world.client.portal.call(partial(measure_outcomes, world.app.state.engine, now=now))


def blocked(world, first: int, count: int, times: int) -> None:  # noqa: F811
    """``count`` session digests of the project, from session ``first`` on, each with a command the harness blocked
    ``times`` times, or none."""
    for index in range(first, first + count):
        body = {**digest_body(), "errors": []}
        if times:
            body["errors"] = [
                {"gen_ai.tool.name": "Bash", "text": "Blocked: sleep 30 followed by tail is not allowed", "n": times}
            ]
        push_digest(world.client, world.headers["owner"], SESSIONS.format(index), body)


def merged_change(world, *, before: int = 1) -> dict:  # noqa: F811
    """Three sessions with ``before`` blocked commands each, then a tier 0 proposal of the environment lens built,
    judged and merged by the hub; the change."""
    blocked(world, 1, 3, before)
    through_the_judge(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "merged": 1}
    return one_change(world)


def merged_at(world) -> object:  # noqa: F811
    c = tables.curator_changes
    return sql(world.db, select(c.c.merged_at))[0][0]


# The model


FIGURES = {
    "night": "2026-10-08",
    "since": "2026-10-01T15:00:00+00:00",
    "until": "2026-10-08T15:00:00+00:00",
    "tools": [{"tool": "Bash", "source": "sessions", "calls": 40, "errors": 6}],
    "environment": [
        {"cause": "harness_blocked", "count": 3, "sessions": ["s-1"], "runs": [], "evidence": ["session:s-1:errors:0"]},
        {"cause": "hub_5xx", "count": 2, "sessions": [], "runs": [41], "evidence": ["run:41:7"]},
    ],
    "failed_runs": [{"cause": "verify_failed", "failed": 2, "lost": 1, "runs": [41, 42], "sample": None}],
    "repeated_commands": [
        {"command": "uv run pytest -q", "times": 4, "sessions": 2, "evidence": ["session:s-1:repeated_commands:0"]}
    ],
    "corrections": [{"session": "s-1", "text": "no", "evidence": "session:s-1:user_turns:1"}],
    "open_items": [{"plan_id": "p", "section": "debt", "key": 0, "text": "x"}],
    "stuck_steps": [],
    "runs": {"done": 4, "failed": 2, "lost": 1, "cancelled": 3, "cost_usd": 1.5},
    "sessions": {"digests": 5, "messages": 60, "models": []},
}


def test_ledger_trigger_takes_the_lens_figure_and_the_entries_its_evidence_points_at():
    texts = ["session:s-1:errors:0", "run:42:3", "session:s-1:repeated_commands:0", "session:s-1:user_turns:1"]
    found = ledger.trigger_of(FIGURES, "environment", texts)
    assert found["night"] == "2026-10-08" and found["activity"] == 5 + 4 + 2 + 1  # sessions, and runs that ran
    assert [(item["key"], item["value"]) for item in found["metrics"]] == [
        ("environment", 5.0),
        ("environment:harness_blocked", 3.0),
        ("failed_runs:verify_failed", 3.0),
        ("repeated:uv run pytest -q", 4.0),
        ("corrections", 1.0),
    ]
    assert found["metrics"][1]["what"] == "failures from the environment of cause harness_blocked"
    by_run = ledger.trigger_of(FIGURES, "tool_errors", ["run:41:7"])  # a run's event points at its causes
    assert [item["key"] for item in by_run["metrics"]] == [
        "tool_errors",
        "environment:hub_5xx",
        "failed_runs:verify_failed",
    ]
    assert ledger.trigger_of(FIGURES, "tech_debt", [])["metrics"][1] == {
        "key": "stuck_steps",
        "what": "steps of active plans that did not move",
        "value": 0.0,
    }
    assert ledger.trigger_of(FIGURES, "docs_drift", [])["metrics"] == []  # a lens the figures do not count
    assert ledger.trigger_of(None, "environment", ["session:s-1:errors:0"]) == {
        "night": None,
        "since": None,
        "until": None,
        "activity": 0,
        "metrics": [],
    }


def _after(environment: int, digests: int, cost: float = 0.0) -> dict:
    return {
        "environment": [{"cause": "harness_blocked", "count": environment, "evidence": ["session:s-9:errors:0"]}],
        "runs": {"done": 0, "failed": 0, "lost": 0, "cost_usd": cost},
        "sessions": {"digests": digests},
    }


@pytest.mark.parametrize(
    "after, result, words",
    [
        (_after(12, 4), "revert", "the figures got worse: failures from the environment"),  # 3 a session, from 1
        (_after(6, 6), "keep", "the figures held"),  # the same per session, over twice the sessions
        (_after(0, 4), "keep", "the figures got better: failures from the environment"),
        (_after(1, 4), "keep", "the figures got better"),
        (_after(30, 2), "unclear", "too little activity to compare: 3 sessions and runs before, 2 after"),
    ],
)
def test_outcome_compares_the_trigger_per_unit_of_activity(after, result, words):
    trigger = {"activity": 3, "metrics": [{"key": "environment", "what": "x", "value": 3.0}]}
    found = ledger.outcome_of(trigger, after, since="a", until="b")
    assert found["result"] == result and words in found["reason"], found
    assert (found["since"], found["until"], found["activity_before"]) == ("a", "b", 3)


def test_outcome_is_unclear_when_mixed_or_without_a_figure_and_a_small_rise_is_no_worse():
    trigger = {
        "activity": 4,
        "metrics": [{"key": "environment", "value": 4.0}, {"key": "corrections", "value": 4.0}],
    }
    mixed = {**_after(16, 4), "corrections": []}
    found = ledger.outcome_of(trigger, mixed)
    assert found["result"] == "unclear" and "some figures got better and some worse" in found["reason"]
    assert [item["change"] for item in found["metrics"]] == ["worse", "better"]
    assert ledger.outcome_of({"activity": 9, "metrics": []}, _after(1, 9))["result"] == "unclear"
    assert ledger.compare("environment", 0, 10, 1, 10) == "same"  # one is below MIN_WORSE
    assert ledger.compare("environment", 0, 10, 2, 10) == "worse"
    assert ledger.compare("cost_usd", 0.1, 10, 0.4, 10) == "same"  # under MIN_WORSE_USD
    assert ledger.compare("environment", 10, 10, 12, 10) == "same"  # within WORSE_FACTOR


def test_outcome_revert_draft_is_an_outcome_plan_the_hub_takes():
    sha = "a" * 40
    draft = ledger.revert_draft(
        proposal_id=7,
        title="A wait helper",
        change_id=3,
        repo="evo-agents",
        merge_sha=sha,
        pr_url="https://github.com/o/r/pull/1",
        reason="the figures got worse: x",
    )
    _check_draft(draft)  # plan.schema.json, in outcome steps
    (step,) = draft["steps"]
    assert draft["id"] == "revert-7-a-wait-helper" and step["repo"] == "evo-agents"
    assert step["verify"] == ledger.revert_verify(sha) and f"{sha}^1" in step["verify"]
    assert f"git revert --no-edit -m 1 {sha}" in step["what"]
    found = {"metrics": [{"key": "environment", "change": "worse"}]}
    assert ledger.worse_evidence(_after(4, 4), found) == ["session:s-9:errors:0"]
    outcome = {"reason": "x", "metrics": [{"what": "times `a | b` ran", "before": 1, "after": 4, "change": "worse"}]}
    summary = ledger.revert_summary(proposal_id=7, change_id=3, merge_sha=sha, pr_url=None, outcome=outcome)
    assert summary.startswith(f"The Curator's change #3 of proposal #7 merged as {sha}. Counted again")
    assert "| times `a \\| b` ran | 1 | 4 |" in summary  # a pipe in a figure's words stays in its cell


def test_circuit_counts_the_jobs_that_went_wrong_at_the_end_of_the_night():
    assert curator.failed_in_a_row([]) == 0
    assert curator.failed_in_a_row([True, True, False]) == 0
    assert curator.failed_in_a_row([True, False, True, True]) == 2
    assert curator.failed_in_a_row([True, True, True]) == 3


def _writes_of(tree: ast.AST) -> list[int]:
    """Lines of ``update(...)`` or ``delete(...)`` calls whose table is curator_ledger, under any name it is bound
    to in the module."""
    names = {"curator_ledger"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
            if node.value.attr == "curator_ledger":
                names |= {target.id for target in node.targets if isinstance(target, ast.Name)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        called = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
        first = node.args[0]
        named = first.attr if isinstance(first, ast.Attribute) else getattr(first, "id", None)
        if called in ("update", "delete") and named in names:
            found.append(node.lineno)
    return found


def test_ledger_lines_are_only_added_in_the_hub():
    writes = {}
    for source in sorted((ROOT / "evo_agents" / "hub").rglob("*.py")):
        if "migrations" in source.parts:
            continue
        lines = _writes_of(ast.parse(source.read_text(encoding="utf-8")))
        if lines:
            writes[str(source.relative_to(ROOT))] = lines
    assert writes == {}, f"the ledger is only added to: {writes}"
    assert _writes_of(ast.parse("lg = tables.curator_ledger\nupdate(lg).values(what='x')\n")) == [2]  # it would see


# The ledger of a proposal


def test_ledger_of_a_tier_0_proposal_from_its_review_to_its_merge(world):  # noqa: F811
    change = merged_change(world)
    proposal_id = change["proposal_id"]
    found = ledger_of(world, proposal_id)
    assert acts(found) == [
        ("proposed", "agent"),
        ("accepted", "user"),
        ("planned", "curator"),
        ("built", "agent"),
        ("pull_opened", "curator"),
        ("judged", "agent"),
        ("merged", "curator"),
    ]
    lines = {line["action"]: line for line in found["lines"]}
    proposal = proposal_of(world, proposal_id)
    assert lines["proposed"]["run_id"] == proposal["run_id"]
    trigger = lines["proposed"]["figures"]
    assert trigger["activity"] == 3 and trigger["metrics"][0] == {
        "key": "environment",
        "what": "failures from the environment",
        "value": 3.0,
    }
    assert lines["accepted"]["actor_login"] == OWNER
    assert lines["planned"]["details"]["branch"] == BRANCH and lines["planned"]["change_id"] == change["id"]
    assert lines["built"]["run_id"] == change["builder_run_id"] and lines["built"]["commit_sha"] == HEAD
    assert lines["pull_opened"]["pr_url"] == change["pr_url"] and lines["pull_opened"]["commit_sha"] == HEAD
    judged_line = lines["judged"]
    assert judged_line["run_id"] == change["judge_run_id"] and judged_line["verdict"]["passed"] is True
    assert judged_line["verdict"]["failures"] == [] and judged_line["commit_sha"] == HEAD
    merged_line = lines["merged"]
    assert (merged_line["commit_sha"], merged_line["before_sha"], merged_line["after_sha"]) == (
        HEAD,
        "0" * 40,  # the fake's main before the merge
        change["merge_sha"],
    )
    assert merged_line["merged_at"] == change["merged_at"] and merged_line["pr_number"] == change["pr_number"]
    assert found["outcome_due_at"] is not None
    assert [line["id"] for line in found["lines"]] == sorted(line["id"] for line in found["lines"])


def test_ledger_reads_as_its_proposal_and_records_each_answer(world):  # noqa: F811
    run_proposal = accepted(world, action="defer")
    proposal_id = run_proposal["id"]
    answered(world.client, world.headers["owner"], proposal_id, "reject", note="not now")
    found = ledger_of(world, proposal_id)
    assert acts(found) == [("proposed", "agent"), ("deferred", "user"), ("rejected", "user")]
    assert found["lines"][1]["what"] == f"{OWNER} deferred it for 7 days"
    assert found["lines"][2]["what"] == f"{OWNER} rejected it: not now"
    assert found["outcome_due_at"] is None and changes(world) == []
    assert ledger_of(world, proposal_id, world.headers["other"])["lines"] == found["lines"]  # a reader of the project
    url = path("proposals", str(proposal_id), "ledger")
    assert world.client.get(url, headers=world.worker["headers"]).status_code == 403  # a worker token
    assert world.client.get(path("proposals", "999", "ledger"), headers=world.headers["owner"]).status_code == 404


def test_ledger_of_a_change_the_hub_fails_for_signs_and_a_builder_that_fails(world):  # noqa: F811
    accepted(world)
    change = one_change(world)
    assert protect(world).json()["protected"] is True
    assert fire(world.client, NIGHT)["builder"] == 1
    spec = claim(world.client, world.worker)
    moved(world.client, world.worker, spec["id"], "running", "failed", error="the agent gave up")
    found = ledger_of(world, change["proposal_id"])
    assert acts(found)[-1] == ("build_failed", "agent")
    assert found["lines"][-1]["what"] == f"Builder run #{spec['id']} ended failed: the agent gave up"
    signs = [{"filename": "tests/test_wait.py", "status": "modified", "patch": "@@ -1,2 +1,1 @@\n-    assert x\n y\n"}]
    builder(world, signs)
    assert moved_on(world) == {"signs": 1}
    found = ledger_of(world, change["proposal_id"])
    assert acts(found)[-3:] == [("built", "agent"), ("pull_opened", "curator"), ("judged", "curator")]
    assert found["lines"][-1]["verdict"]["passed"] is False and found["lines"][-1]["run_id"] is None
    assert found["lines"][-1]["what"].startswith("The hub failed it before any Judge")


def test_ledger_of_a_pull_request_merged_by_hand_and_one_closed(world):  # noqa: F811
    accepted(world, kind="fix", paths=[{"repo": "evo-agents", "path": "tests/test_wait.py"}])
    builder(world)
    moved_on(world)
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}  # tier 1 waits for its owner
    world.github.merge_by_hand(MINE, "evo-agents", BRANCH, OWNER, HAND_MERGE)
    assert moved_on(world) == {}  # a pull request left open is read again only after an hour
    c = tables.curator_changes
    sql(world.db, update(c).values(updated_at=func.now() - timedelta(hours=2)))
    assert moved_on(world) == {"merged_by_hand": 1}
    change = one_change(world)
    assert (change["state"], change["merge_sha"]) == ("merged", HAND_MERGE) and change["merged_at"]
    line = ledger_of(world, change["proposal_id"])["lines"][-1]
    assert (line["action"], line["actor"], line["actor_login"]) == ("merged", "user", OWNER)
    assert (line["before_sha"], line["after_sha"]) == ("0" * 40, HAND_MERGE)
    assert line["what"] == f"{OWNER} merged pull request #1 into main as {HAND_MERGE}"
    assert ledger_of(world, change["proposal_id"])["outcome_due_at"] is not None
    sql(world.db, update(c).values(state="open", merged_at=None, merge_sha=None, updated_at=func.now() - timedelta(1)))
    world.github.close_by_hand(MINE, "evo-agents", BRANCH)
    world.github.pull_of(MINE, "evo-agents", BRANCH).merged = False
    assert moved_on(world) == {"closed": 1}
    assert one_change(world)["state"] == "closed"
    assert acts(ledger_of(world, change["proposal_id"]))[-1] == ("closed", "user")


def test_ledger_a_pull_request_left_open_that_github_refuses_is_read_again_an_hour_later(world):  # noqa: F811
    accepted(world, kind="fix", paths=[{"repo": "evo-agents", "path": "tests/test_wait.py"}])
    builder(world)
    moved_on(world)
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    c = tables.curator_changes
    sql(world.db, update(c).values(pr_number=99, updated_at=func.now() - timedelta(hours=2)))  # GitHub has no #99
    reads = len([r for r in world.github.requests if r.method == "GET" and r.path.endswith("/pulls/99")])
    assert moved_on(world) == {}
    assert len([r for r in world.github.requests if r.method == "GET" and r.path.endswith("/pulls/99")]) == reads + 1
    assert sql(world.db, select(c.c.updated_at > func.now() - timedelta(minutes=5)))[0][0] is True
    assert moved_on(world) == {}  # not read again before an hour
    assert len([r for r in world.github.requests if r.method == "GET" and r.path.endswith("/pulls/99")]) == reads + 1
    assert one_change(world)["state"] == "open"


# The outcome


def test_outcome_waits_outcome_days_then_keeps_a_change_whose_figures_got_better(world):  # noqa: F811
    change = merged_change(world)
    blocked(world, 10, 3, 0)  # after the merge: three sessions, nothing blocked
    when = merged_at(world)
    assert measured(world, when + timedelta(days=6)) == {"waiting": 1}
    assert measured(world, when + timedelta(days=7, minutes=1)) == {"keep": 1}
    assert measured(world, when + timedelta(days=8)) == {}  # once
    found = ledger_of(world, change["proposal_id"])
    line = found["lines"][-1]
    assert (line["action"], line["actor"], line["outcome"], line["commit_sha"]) == (
        "outcome",
        "curator",
        "keep",
        change["merge_sha"],
    )
    figures = line["figures"]
    assert (figures["activity_before"], figures["activity_after"]) == (3, 3)
    assert figures["metrics"][0]["before"] == 3.0 and figures["metrics"][0]["after"] == 0.0
    assert figures["metrics"][0]["change"] == "better" and line["details"] == {"outcome_days": 7}
    assert found["outcome_due_at"] is None
    a = tables.audit
    (target,) = sql(world.db, select(a.c.target).where(a.c.action == "curator.outcome"))[0]
    assert target == f"{PROJECT} proposal:{change['proposal_id']} change:{change['id']} outcome=keep"


def test_outcome_after_the_charters_outcome_days_and_unclear_without_activity(world):  # noqa: F811
    written(world.client, world.headers["owner"], curator_charter(outcome_days=2))
    change = merged_change(world)
    when = merged_at(world)
    assert measured(world, when + timedelta(days=1)) == {"waiting": 1}
    assert measured(world, when + timedelta(days=2, minutes=1)) == {"unclear": 1}
    line = ledger_of(world, change["proposal_id"])["lines"][-1]
    assert line["outcome"] == "unclear" and "too little activity to compare" in line["what"]
    assert line["details"] == {"outcome_days": 2}


def test_outcome_worse_figures_make_the_hub_propose_a_revert_of_tier_1(world):  # noqa: F811
    change = merged_change(world)
    blocked(world, 10, 3, 4)  # after the merge: four blocked commands a session, from one
    assert measured(world, merged_at(world) + timedelta(days=8)) == {"revert": 1}
    original = ledger_of(world, change["proposal_id"])["lines"][-1]
    assert original["outcome"] == "revert" and "the figures got worse" in original["what"]
    revert_id = original["details"]["revert_proposal_id"]
    revert = proposal_of(world, revert_id)
    assert (revert["kind"], revert["tier"], revert["state"], revert["revert_of"]) == (
        "revert",
        1,
        "open",
        change["proposal_id"],
    )
    assert revert["title"].startswith("Revert: ") and revert["inbox_at"] is not None
    sessions = sorted(item["session_id"] for item in revert["evidence"])  # the digests the worse figures count
    assert sessions == [SESSIONS.format(index) for index in (10, 11, 12)]
    assert {item["field"] for item in revert["evidence"]} == {"errors"}
    assert revert["evidence_count"] == 3 and revert["paths"] == [{"repo": "evo-agents", "path": "tests/test_wait.py"}]
    (step,) = revert["plan"]["steps"]
    assert (
        step["verify"] == ledger.revert_verify(change["merge_sha"])
        and "| failures from the environment |" in (revert["summary"])
    )
    first = ledger_of(world, revert_id)["lines"][0]
    assert (first["action"], first["actor"], first["commit_sha"]) == ("proposed", "curator", change["merge_sha"])
    assert first["figures"]["metrics"][0]["value"] == 12.0
    n = tables.notifications
    (note,) = sql(world.db, select(n.c.kind, n.c.details, n.c.link).where(n.c.proposal_id == revert_id))
    assert note[0] == "proposal" and note[1]["tier"] == 1 and note[1]["revert_of"] == change["proposal_id"]

    answered(world.client, world.headers["owner"], revert_id, "accept")  # it becomes the Curator's plan
    planned = next(item for item in changes(world) if item["proposal_id"] == revert_id)
    assert planned["state"] == "planned" and planned["tier"] == 1 and planned["branch"].startswith("curator/")
    plan = world.client.get(f"/v1/projects/{PROJECT}/plans/{planned['plan_id']}", headers=world.headers["owner"])
    assert plan.json()["body"]["steps"][0]["verify"] == ledger.revert_verify(change["merge_sha"])
    assert acts(ledger_of(world, revert_id)) == [("proposed", "curator"), ("accepted", "user"), ("planned", "curator")]


def test_outcome_a_revert_counts_as_a_job_of_the_night_that_went_wrong(world):  # noqa: F811
    change = merged_change(world)
    blocked(world, 10, 3, 4)
    assert measured(world, merged_at(world) + timedelta(days=8)) == {"revert": 1}
    zone = curator_charter()["window"]["timezone"]
    local = sql(world.db, select(func.timezone(zone, func.now())))[0][0]
    tonight = curator.night_of(local, time(22, 0))
    assert "circuit" not in fire(world.client, None)  # one revert alone is one job gone wrong
    r = tables.runs
    sql(
        world.db,
        update(r)
        .values(state="failed", schedule_night=tonight, finished_at=func.now(), error="the agent gave up")
        .where(r.c.id == change["judge_run_id"]),
    )
    assert fire(world.client, None) == {"circuit": 1}
    (schedule,) = status_of(world.client, world.headers["owner"])["schedules"]
    assert schedule["paused_by"] is None and f"run #{change['judge_run_id']} failed" in schedule["pause_reason"]
    assert f"the change of proposal #{change['proposal_id']} got worse figures" in schedule["pause_reason"]


# The circuit breaker


def notices(db, kind: str) -> list[tuple]:
    n = tables.notifications
    query = select(n.c.user_id, n.c.title, n.c.details, n.c.link).where(n.c.notice_kind == kind).order_by(n.c.id)
    return sql(db, query)


def test_circuit_breaker_pauses_the_night_shift_after_two_failed_runs_in_a_row(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body(max_runs_per_night=10))
    assert fire(client, NIGHT) == {"queued": 1}
    first = scheduled(hub_db)[-1]["id"]
    ended(client, night.worker, first)
    assert fire(client, NIGHT) == {"queued": 1}  # one failure: the night goes on
    second = scheduled(hub_db)[-1]["id"]
    ended(client, night.worker, second)
    assert fire(client, NIGHT) == {"circuit": 1}
    assert fire(client, NIGHT) == {"paused": 1} and len(scheduled(hub_db)) == 2
    status = status_of(client, headers["reader"])
    assert status["paused"] is True and status["state"] == "paused"
    (schedule,) = status["schedules"]
    assert schedule["paused_by"] is None and schedule["paused_at"] is not None
    reason = schedule["pause_reason"]
    assert reason.startswith(
        f"The circuit breaker paused the night shift of {PROJECT}: run #{first}, run #{second} failed"
    )
    assert f"2 jobs of the night of {THE_NIGHT.isoformat()} in a row (max_failed_in_a_row 2)" in reason
    owner_id = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    ((user, title, details, link),) = notices(hub_db, "curator_paused")
    assert (user, link) == (owner_id, f"/p/{PROJECT}/curator")
    assert title == f"The night shift of {PROJECT} paused itself: 2 jobs in a row went wrong"
    assert details["runs"] == [first, second] and details["night"] == THE_NIGHT.isoformat()
    a = tables.audit
    ((actor, target),) = sql(hub_db, select(a.c.actor_id, a.c.target).where(a.c.action == "curator.circuit"))
    assert actor is None and target == f"{PROJECT} night={THE_NIGHT.isoformat()} failed=2 jobs=run:{first},run:{second}"

    resumed = client.post(charter_path("resume"), headers=headers["owner"])
    assert resumed.status_code == 200 and resumed.json()["schedules"][0]["pause_reason"] is None
    assert fire(client, NIGHT) == {"queued": 1}  # resuming starts the count again
    ended(client, night.worker, scheduled(hub_db)[-1]["id"])
    assert fire(client, NIGHT) == {"queued": 1}


def test_circuit_a_done_run_breaks_the_row_and_a_cancelled_one_counts_neither_way(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body(max_runs_per_night=10, circuit_breaker={"max_failed_in_a_row": 2}))
    assert fire(client, NIGHT) == {"queued": 1}
    ended(client, night.worker, scheduled(hub_db)[-1]["id"])
    assert fire(client, NIGHT) == {"queued": 1}
    done = scheduled(hub_db)[-1]["id"]
    assert claim(client, night.worker)["id"] == done
    moved(client, night.worker, done, "running", "verifying", "done")
    assert fire(client, NIGHT) == {"queued": 1}
    ended(client, night.worker, scheduled(hub_db)[-1]["id"])  # failed, done, failed
    assert fire(client, NIGHT) == {"queued": 1}
    cancelled = scheduled(hub_db)[-1]["id"]
    assert control(client, headers["owner"], cancelled, "cancel").status_code == 200
    assert fire(client, NIGHT) == {"queued": 1}  # failed, done, failed, cancelled
    ended(client, night.worker, scheduled(hub_db)[-1]["id"])
    assert fire(client, NIGHT) == {"circuit": 1}  # failed, done, failed, cancelled, failed
    assert notices(hub_db, "curator_paused")[0][2]["failed_in_a_row"] == 2


def test_circuit_breaker_of_one_and_a_night_that_ended_still_trips(night, hub_db):  # noqa: F811
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body(circuit_breaker={"max_failed_in_a_row": 1}))
    assert fire(client, NIGHT) == {"queued": 1}
    run_id = scheduled(hub_db)[-1]["id"]
    ended(client, night.worker, run_id)
    day = NIGHT + timedelta(hours=10)  # the window closed: the night is still the one that opened last
    assert fire(client, day) == {"circuit": 1}
    assert status_of(client, headers["owner"])["paused"] is True
    assert notices(hub_db, "curator_paused")[0][2]["runs"] == [run_id]


# Schema 0018


def test_ledger_columns_of_0018_hold_together(world):  # noqa: F811
    change = merged_change(world)
    db = world.db
    lg = tables.curator_ledger
    base = {"project_id": _project_id(db), "proposal_id": change["proposal_id"]}
    bad = [
        {"action": "built", "actor": "agent", "what": "no run"},  # an agent's line names its run
        {"action": "proposed", "actor": "curator", "what": "x", "outcome": "keep"},  # an outcome only on its line
        {"action": "outcome", "actor": "curator", "what": "x"},  # and that line has one
        {"action": "outcome", "actor": "curator", "what": "x", "outcome": "keep"},  # of a change
        {"action": "proposed", "actor": "curator", "what": "x", "actor_id": 1},  # a member only on a user's line
        {"action": "proposed", "actor": "curator", "what": "x", "commit_sha": "main"},
        {"action": "proposed", "actor": "curator", "what": "two\nlines"},
        {"action": "copied", "actor": "curator", "what": "x"},
        {"action": "proposed", "actor": "robot", "what": "x"},
    ]
    for values in bad:
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(db, insert(lg).values(**base, **values))
    good = {**base, "action": "outcome", "actor": "curator", "what": "x", "outcome": "keep", "change_id": change["id"]}
    sql(db, insert(lg).values(**good))
    with pytest.raises(psycopg.errors.UniqueViolation):  # one outcome a change
        sql(db, insert(lg).values(**good))
    s, p = tables.schedules, tables.proposals
    with pytest.raises(psycopg.errors.CheckViolation):  # paused by nobody
        sql(db, update(s).values(paused_at=func.now()))
    with pytest.raises(psycopg.errors.CheckViolation):  # a reason while it runs
        sql(db, update(s).values(pause_reason="why"))
    with pytest.raises(psycopg.errors.CheckViolation):  # only a revert reverts
        sql(db, update(p).values(revert_of=change["proposal_id"]))


def _project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def test_ledger_migration_0018_goes_down_to_0017_and_up_again(world):  # noqa: F811
    change = merged_change(world)
    blocked(world, 10, 3, 4)
    assert measured(world, merged_at(world) + timedelta(days=8)) == {"revert": 1}
    db = world.db
    s, p, n = tables.schedules, tables.proposals, tables.notifications
    sql(db, update(s).values(paused_at=func.now(), pause_reason="the circuit breaker"))
    owner_id = sql(db, select(s.c.owner_id))[0][0]
    sql(
        db,
        insert(n).values(
            user_id=owner_id, kind="notice", notice_kind="curator_paused", title="paused", project_id=_project_id(db)
        ),
    )
    move_to(db, "0017", down=True)
    assert sql(db, select(func.count()).select_from(p).where(p.c.id != change["proposal_id"])) == [(0,)]
    assert sql(db, select(s.c.paused_by)) == [(owner_id,)]  # still paused, as its owner's pause
    assert sql(db, select(func.count()).select_from(n).where(n.c.title == "paused")) == [(0,)]
    move_to(db, "0018")
    assert sql(db, select(func.count()).select_from(tables.curator_ledger)) == [(0,)]


def test_outcome_job_runs_every_ten_minutes_and_is_queued_at_most_once():
    periodic = {item.task.name: item for item in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.CURATOR_OUTCOMES].cron == "*/10 * * * *"
    assert queue.tasks[jobs.CURATOR_OUTCOMES].queueing_lock == jobs.CURATOR_OUTCOMES == "curator.outcomes"
    assert "curator_paused" in runs.NOTICE_KINDS
