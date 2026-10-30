"""The project's agent answers the Curator's proposals for their owner (``evo_agents.hub.server.curator_threads``):
criterion a1 of the plan proposals-answer-themselves.

With a charter whose answer_by is agent, each proposal a review run records open, and each deferred one that opens
again, has a thread in the project's triage chat that mentions the project's default agent: the proposal's facts, its
links, the command and the skill the agent answers with. The agent answers it with ``evo-agents worker curator answer``
(POST /v1/worker/runs/{id}/curator/answer) for the owner, as the owner's answer on the web does, so accepting a tier the
charter plans makes the Curator's plan. The route refuses another proposal than the thread's, a chat run in no
proposal's thread, a dispatcher who is no admin, a proposal answered already and a charter that has its admins answer
again. Each answer writes the audit row and the ledger line with the agent and the chat run, a ``Decision:`` line in the
thread, and the morning brief lists it. A charter without answer_by (owner) opens no thread. The world, GitHub and its
helpers are ``tests.hub.test_curator_changes``'s.

And criterion a4: a change the hub leaves open for its owner (a tier outside auto_merge, a sign of a protected path) is
told in its proposal's thread, with the reason and the pull request, and the agent merges it with ``evo-agents worker
curator merge`` (POST /v1/worker/runs/{id}/curator/merge) at the head the Judge passed, with the workers' App; the route
refuses a change of another proposal, a change the Judge did not pass, a head that moved, a sign of score hacking, a
paused night shift, K5 and a charter back to owner, with the reason. Each merge writes the audit row, the ledger line,
the owner's notice and a ``Decision:`` line, and the morning brief lists it."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.server import autonomy_gates
from evo_agents.hub.server.collect import reopen_deferred
from evo_agents.hub.server.curator_threads import SKILL
from evo_agents.worker import curator_cli
from tests.hub.live import sql
from tests.hub.test_brief import BRIEF, body_of, briefed, briefs_of
from tests.hub.test_chat_runs import chat_worker, thread
from tests.hub.test_credentials_api import (
    MINE,
    app_key,  # noqa: F401 (a fixture)
)
from tests.hub.test_curator import grant, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    BRANCH,
    CLEAN,
    GREEN,
    HEAD,
    MOVED,
    SUCCESS,
    builder,
    calls,
    curator_charter,
    judged,
    moved_on,
    one_change,
    proposal_of,
    review_run,
    world,
)
from tests.hub.test_curator_ledger import acts, ledger_of
from tests.hub.test_review_runs import DRAFT, answered, proposed
from tests.hub.test_runs import PROJECT, claim, moved

FEATURE = {"kind": "feature", "paths": [{"repo": "evo-agents", "path": "tests/test_wait.py"}]}  # tier 2, its kind's
NOTE = "the run's log shows the blocked wait at the commit cited"


def by_agent(world, **changes) -> None:  # noqa: F811
    written(world.client, world.headers["owner"], curator_charter(answer_by="agent", **changes))


def opened(world, *titles: str) -> tuple[SimpleNamespace, list[dict]]:  # noqa: F811
    """A worker of owner that takes chat runs, then the night's review run with a tier 2 proposal for each of
    ``titles`` recorded open; (the chat worker, the proposals)."""
    chat = chat_worker(world.client, world.headers["owner"], "mac-book", slots=4)
    run_id = review_run(world)
    evidence = [{"kind": "run", "run_id": run_id, "seq": 1}]
    made = [
        proposed(world.client, world.worker, run_id, title=title, evidence=evidence, plan=DRAFT, **FEATURE)
        for title in titles or ("A wait helper instead of sleep and tail",)
    ]
    return SimpleNamespace(**chat), made


def thread_of(world, proposal_id: int) -> list[tuple]:  # noqa: F811
    t = tables.proposal_threads
    query = select(t.c.chat_id, t.c.root_message_id, t.c.agent_run_id).where(t.c.proposal_id == proposal_id)
    return sql(world.db, query)


def said(world, proposal_id: int) -> list[str]:  # noqa: F811
    ((chat_id, root_id, _),) = thread_of(world, proposal_id)
    return [message["body"] for message in thread(world.client, world.headers["owner"], chat_id, root_id)]


def agent_of(world, chat, proposal_id: int) -> int:  # noqa: F811
    """Claim and start the chat run the thread of ``proposal_id`` asked for, on ``chat``; its id."""
    ((_, _, agent_run),) = thread_of(world, proposal_id)
    assert agent_run is not None, said(world, proposal_id)
    for _ in range(4):
        spec = claim(world.client, vars(chat))
        if spec and spec["id"] == agent_run:
            moved(world.client, vars(chat), agent_run, "running")
            return agent_run
    raise AssertionError(f"chat run {agent_run} was not claimed")


def answer(world, chat, agent_run: int, proposal_id: int, action: str = "accept", **body):  # noqa: F811
    sent = {"proposal_id": proposal_id, "action": action, "note": NOTE, **body}
    return world.client.post(f"/v1/worker/runs/{agent_run}/curator/answer", json=sent, headers=chat.headers)


def audited(world) -> list[str]:  # noqa: F811
    a = tables.audit
    query = select(a.c.target).where(a.c.action == "curator.proposal").order_by(a.c.id)
    return [row[0] for row in sql(world.db, query)]


def test_a_charter_without_answer_by_opens_no_thread_and_the_admin_answers_as_before(world):  # noqa: F811
    _, (made,) = opened(world)
    assert thread_of(world, made["id"]) == []
    assert answered(world.client, world.headers["owner"], made["id"], "accept")["state"] == "accepted"
    assert audited(world) == [f"{PROJECT} proposal:{made['id']} run:{made['run_id']} answer=accept"]


def test_a_proposal_recorded_open_gets_a_thread_with_its_facts_that_mentions_the_agent(world):  # noqa: F811
    by_agent(world)
    _, (made,) = opened(world)
    ((chat_id, root_id, agent_run),) = thread_of(world, made["id"])
    assert agent_run is not None
    (root,) = said(world, made["id"])
    for line in (
        f"Curator proposal #{made['id']} of {PROJECT} waits for an answer: A wait helper instead of sleep and tail",
        "Tier 2 (feature, lens environment)",
        "Paths: evo-agents:tests/test_wait.py",
        f"run:{made['run_id']}:1",
        "Draft plan curator-fix-sleep: 1 Wait helper.",
        "makes its draft the Curator's plan",
        f"/p/{PROJECT}/curator/proposals/{made['id']}",
        f"`evo-agents worker curator answer {made['id']} accept|reject|defer --note TEXT`",
        SKILL,
    ):
        assert line in root, line
    assert root.rstrip().splitlines()[-1].startswith("@")
    c = tables.chats
    assert sql(world.db, select(c.c.title).where(c.c.id == chat_id)) == [(f"Triage: {PROJECT}",)]
    r = tables.runs
    assert sql(world.db, select(r.c.kind, r.c.state).where(r.c.id == agent_run)) == [("chat", "queued")]


def test_the_agent_accepts_for_the_owner_and_the_draft_becomes_the_curators_plan_with_every_trace(world):  # noqa: F811
    by_agent(world)
    chat, (made,) = opened(world)
    agent_run = agent_of(world, chat, made["id"])
    done = answer(world, chat, agent_run, made["id"])
    assert done.status_code == 200, done.text
    proposal = done.json()["proposal"]
    assert (proposal["state"], proposal["answered_by"], proposal["note"]) == ("accepted", "owner", NOTE)
    change = one_change(world)
    assert (change["proposal_id"], change["state"]) == (made["id"], "planned")
    assert proposal["change"]["id"] == change["id"]
    (target,) = audited(world)
    assert target.startswith(f"{PROJECT} proposal:{made['id']} run:{made['run_id']} answer=accept via=@")
    assert target.endswith(f" in chat run {agent_run}")
    ledger = ledger_of(world, made["id"])
    assert acts(ledger)[-2:] == [("accepted", "user"), ("planned", "curator")]
    line = ledger["lines"][-2]
    assert line["actor_login"] == "owner" and line["details"]["chat_run"] == agent_run
    assert line["details"]["agent"].startswith("@") and NOTE in line["what"]
    decision = said(world, made["id"])[-1]
    assert decision.startswith(f"Decision: accepted proposal #{made['id']} (tier 2) for owner: {NOTE}")
    assert f"Change #{change['id']}: planned, plan {change['plan_id']}." in decision
    assert done.json()["message_id"] > 0


def test_the_route_refuses_another_proposal_a_run_in_no_proposals_thread_and_an_answered_one(world):  # noqa: F811
    by_agent(world)
    chat, (first, second) = opened(world, "A wait helper", "A second helper")
    agent_run = agent_of(world, chat, first["id"])
    other = answer(world, chat, agent_run, second["id"])
    assert other.status_code == 403 and f"thread of proposal {first['id']}" in other.json()["message"]
    review = answer(world, SimpleNamespace(headers=world.worker["headers"]), first["run_id"], first["id"])
    assert review.status_code == 404
    assert answer(world, chat, agent_run, first["id"], "reject").status_code == 200
    again = answer(world, chat, agent_run, first["id"])
    assert again.status_code == 409 and "is rejected" in again.json()["message"]
    assert proposal_of(world, second["id"])["state"] == "open"
    assert len(audited(world)) == 1
    refused = answer(world, chat, agent_run, first["id"], "accept", defer_days=3)
    assert refused.status_code == 422


def test_the_route_refuses_a_dispatcher_who_is_no_admin_and_a_charter_back_to_owner(world):  # noqa: F811
    by_agent(world)
    chat, (made,) = opened(world)
    agent_run = agent_of(world, chat, made["id"])
    written(world.client, world.headers["owner"], curator_charter())
    back = answer(world, chat, agent_run, made["id"])
    assert back.status_code == 409 and "answer_by owner" in back.json()["message"]
    by_agent(world)
    grant(world.client, world.headers, "owner", "writer")
    writer = answer(world, chat, agent_run, made["id"])
    assert writer.status_code == 403 and "holds writer" in writer.json()["message"]
    assert proposal_of(world, made["id"])["state"] == "open"
    assert audited(world) == []
    assert not any(line.startswith("Decision:") for line in said(world, made["id"]))


def test_a_deferred_proposal_open_again_asks_the_agent_in_the_same_thread(world):  # noqa: F811
    by_agent(world)
    chat, (made,) = opened(world)
    agent_run = agent_of(world, chat, made["id"])
    deferred = answer(world, chat, agent_run, made["id"], "defer", defer_days=3)
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["proposal"]["state"] == "deferred"
    assert "It opens again at" in said(world, made["id"])[-1]
    ((chat_id, root_id, _),) = thread_of(world, made["id"])
    p = tables.proposals
    past = func.now() - func.make_interval(0, 0, 0, 1)
    sql(world.db, update(p).where(p.c.id == made["id"]).values(deferred_until=past))
    assert world.client.portal.call(partial(_reopened, world.app.state.engine)) == 1
    assert proposal_of(world, made["id"])["state"] == "open"
    ((chat_again, root_again, asked),) = thread_of(world, made["id"])
    assert (chat_again, root_again) == (chat_id, root_id) and asked is not None
    last = said(world, made["id"])[-1]
    assert f"Curator proposal #{made['id']} of {PROJECT} is open again after its defer" in last
    assert last.rstrip().splitlines()[-1].startswith("@")


async def _reopened(engine) -> int:
    async with engine.begin() as conn:
        return await reopen_deferred(conn)


def test_an_owner_who_is_no_admin_gets_the_thread_without_the_agent(world):  # noqa: F811
    by_agent(world)
    grant(world.client, world.headers, "owner", "writer")
    _, (made,) = opened(world)
    ((_, _, agent_run),) = thread_of(world, made["id"])
    assert agent_run is None
    (root,) = said(world, made["id"])
    assert "holds writer on" in root and not root.rstrip().splitlines()[-1].startswith("@")


def test_the_morning_brief_lists_the_answers_of_the_agent(world):  # noqa: F811
    by_agent(world)
    chat, (made,) = opened(world)
    agent_run = agent_of(world, chat, made["id"])
    assert answer(world, chat, agent_run, made["id"], "reject").status_code == 200
    moved(world.client, world.worker, made["run_id"], "verifying", "done")
    assert briefed(world.client, BRIEF) == {"brief": 1}
    facts = body_of(world.db)
    ((item,),) = (facts["agent_answers"]["listed"],)
    assert facts["agent_answers"]["total"] == 1
    assert (item["proposal_id"], item["state"], item["chat_run"], item["note"]) == (
        made["id"],
        "rejected",
        agent_run,
        NOTE,
    )
    (note,) = briefs_of(world.client, world.headers["owner"])
    assert "Proposals the project's agent answered for you: 1." in note["body"]
    assert f"- proposal #{made['id']} rejected by @" in note["body"]


def test_worker_curator_answer_sends_the_answer_and_prints_one_line(monkeypatch, capsys):
    sent = []

    def call(run, route, body):
        sent.append((route, body))
        return {"message_id": 9, "proposal": {"id": 5, "state": "deferred", "change": None}}

    monkeypatch.setattr(curator_cli, "agent_run", lambda command: SimpleNamespace(run_id=7))
    monkeypatch.setattr(curator_cli, "call", call)
    args = SimpleNamespace(proposal=5, action="defer", note="wait for the kg eval", days=3)
    assert curator_cli.cmd_answer(args) == 0
    assert sent == [
        ("curator/answer", {"proposal_id": 5, "action": "defer", "note": "wait for the kg eval", "defer_days": 3})
    ]
    assert capsys.readouterr().out.strip() == "Proposal #5 is deferred; the Decision line is message #9."


# The agent merges a change the hub left open (a4)

PROTECTED = {"filename": "curator.yaml", "status": "modified", "patch": "@@ -1 +1 @@\n-a: 1\n+a: 2\n"}
ASSERT_REMOVED = {
    "filename": "tests/test_wait.py",
    "status": "modified",
    "patch": "@@ -1,2 +1,2 @@\n def test_wait():\n-    assert wait_for()\n+    wait_for()\n",
}


def merge(world, chat, agent_run: int, change_id: int):  # noqa: F811
    return world.client.post(
        f"/v1/worker/runs/{agent_run}/curator/merge", json={"change_id": change_id}, headers=chat.headers
    )


def built(world, *, files=None, end_agent: bool = True, **charter) -> tuple[SimpleNamespace, dict, int]:  # noqa: F811
    """With answer_by agent (and ``charter``), the tier 2 proposal answered by the agent, its review run ended, the
    Curator's plan built with ``files`` and its pull request opened; (the chat worker, the proposal, the chat run that
    answered it, ended with ``end_agent``)."""
    by_agent(world, **charter)
    chat, (made,) = opened(world)
    moved(world.client, world.worker, made["run_id"], "verifying", "done")
    agent_run = agent_of(world, chat, made["id"])
    assert answer(world, chat, agent_run, made["id"]).status_code == 200
    if end_agent:
        moved(world.client, vars(chat), agent_run, "verifying", "done")
    builder(world, files)
    return chat, made, agent_run


def left_open(world, *, files=None, **charter) -> tuple[SimpleNamespace, dict, int]:  # noqa: F811
    """``built``, judged passed with CI green, and left open by the hub's merge; (the chat worker, the proposal, the
    chat run the hub's line in the thread asked)."""
    chat, made, _ = built(world, **charter)
    assert moved_on(world) == {"opened": 1}
    assert judged(world)["passed"] is True
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    if files is not None:
        world.github.repos[(MINE.lower(), "evo-agents")].files[BRANCH] = files
    assert moved_on(world) == {"checked": 1, "open": 1}
    return chat, made, agent_of(world, chat, made["id"])


def merged_audit(world) -> list[str]:  # noqa: F811
    a = tables.audit
    return [row[0] for row in sql(world.db, select(a.c.target).where(a.c.action == "curator.merge").order_by(a.c.id))]


def test_merge_a_change_left_open_for_its_tier_is_told_in_the_thread_and_the_agent_merges_it(world):  # noqa: F811
    chat, made, agent_run = left_open(world)
    change = one_change(world)
    assert change["state"] == "open" and "tier 2 waits for its owner" in change["reason"]
    told = said(world, made["id"])[-1]
    for line in (
        f"Change #{change['id']} of Curator proposal #{made['id']} of {PROJECT} (tier 2) is left open for its owner",
        f"Pull request: {change['pr_url']}, head {HEAD[:12]}, into main. The Judge passed it.",
        f"`evo-agents worker curator merge {change['id']}`",
        SKILL,
    ):
        assert line in told, line
    assert told.rstrip().splitlines()[-1].startswith("@")
    done = merge(world, chat, agent_run, change["id"])
    assert done.status_code == 200, done.text
    view = done.json()["change"]
    assert (view["state"], view["head_sha"]) == ("merged", HEAD) and view["merge_sha"]
    assert world.github.merges == [(MINE, "evo-agents", 1, HEAD, "workers")]
    (target,) = merged_audit(world)
    assert f"sha={view['merge_sha']} via=@" in target and target.endswith(f" in chat run {agent_run}")
    line = ledger_of(world, made["id"])["lines"][-1]
    assert (line["action"], line["actor"], line["actor_login"]) == ("merged", "user", "owner")
    assert line["details"]["chat_run"] == agent_run and line["details"]["agent"].startswith("@")
    assert f"for owner (chat run {agent_run})" in line["what"]
    decision = said(world, made["id"])[-1]
    assert decision.startswith(f"Decision: merged pull request #1 of evo-agents (change #{change['id']}, proposal ")
    assert f"for owner at {HEAD}, as {view['merge_sha']}." in decision
    n = tables.notifications
    (note,) = sql(world.db, select(n.c.title, n.c.body).where(n.c.notice_kind == "merge_default_branch"))
    assert note[0].startswith("@") and note[0].endswith("merged pull request #1 of evo-agents into main")
    assert f"for owner in chat run {agent_run}" in note[1]
    assert moved_on(world) == {}  # the job leaves a merged change alone
    again = merge(world, chat, agent_run, change["id"])
    assert again.status_code == 409 and "it is merged" in again.json()["message"]
    assert briefed(world.client, BRIEF) == {"brief": 1}
    merges = body_of(world.db)["agent_merges"]
    assert merges["total"] == 1 and merges["listed"][0]["chat_run"] == agent_run
    (brief,) = briefs_of(world.client, world.headers["owner"])
    assert "Pull requests the project's agent merged for you: 1." in brief["body"]
    assert f"- change #{change['id']} of proposal #{made['id']}: {change['pr_url']} merged by @" in brief["body"]


def test_merge_a_passed_change_whose_only_signs_are_protected_paths(world):  # noqa: F811
    chat, made, agent_run = left_open(world, files=[*CLEAN, PROTECTED], auto_merge=[0, 1, 2, 3])
    change = one_change(world)
    assert "protected paths: protected_path" in change["reason"] and world.github.merges == []
    assert said(world, made["id"])[-1].rstrip().splitlines()[-1].startswith("@")
    done = merge(world, chat, agent_run, change["id"])
    assert done.status_code == 200, done.text
    assert done.json()["change"]["state"] == "merged"
    assert world.github.merges == [(MINE, "evo-agents", 1, HEAD, "workers")]


def test_merge_refusals_say_why_and_merge_nothing(world, monkeypatch):  # noqa: F811
    chat, made, agent_run = left_open(world)
    change_id = one_change(world)["id"]

    def refused(code: int, words: str, run_id: int = agent_run, change: int = change_id) -> None:
        response = merge(world, chat, run_id, change)
        assert response.status_code == code and words in response.json()["message"], response.text

    refused(404, f"no change #{change_id + 7}", change=change_id + 7)
    repo = world.github.repos[(MINE.lower(), "evo-agents")]
    repo.files[BRANCH] = [ASSERT_REMOVED]
    refused(409, "signs of score hacking or protected paths: assert_removed")
    repo.files[BRANCH] = CLEAN
    world.github.push(MINE, "evo-agents", BRANCH, MOVED)
    refused(409, "not the commit the Judge passed")
    world.github.push(MINE, "evo-agents", BRANCH, HEAD, files=CLEAN)
    s = tables.schedules
    night = s.c.kind == "night_shift"
    sql(world.db, update(s).where(night).values(paused_at=func.now(), pause_reason="the circuit breaker"))
    refused(409, "the night shift of the project is paused")
    sql(world.db, update(s).where(night).values(paused_at=None, pause_reason=None))
    gated = []

    async def deny(conn, run_id, klass, **act):
        gated.append((run_id, klass, act["key"]))
        return autonomy_gates.Verdict(False, "deny", f"{act['what']}: K5 is deny for owner")

    with monkeypatch.context() as patched:
        patched.setattr(autonomy_gates, "check", deny)
        refused(409, "merge pull request #1 of evo-agents into main: K5 is deny for owner")
    assert gated == [(agent_run, "K5", "merge evo-agents#1")]
    written(world.client, world.headers["owner"], curator_charter())
    refused(409, "answer_by owner")
    by_agent(world)
    grant(world.client, world.headers, "owner", "writer")
    refused(403, "holds writer")
    grant(world.client, world.headers, "owner", "admin")
    assert world.github.merges == [] and calls(world.github, "PUT", "/merge") == [] and merged_audit(world) == []
    assert one_change(world)["state"] == "open"
    assert merge(world, chat, agent_run, change_id).status_code == 200  # nothing left in the way


def test_merge_a_change_of_another_proposals_thread_is_refused(world):  # noqa: F811
    by_agent(world)
    chat, (first, second) = opened(world, "A wait helper", "A second helper")
    runs_of = {made["id"]: thread_of(world, made["id"])[0][2] for made in (first, second)}
    for _ in range(4):
        spec = claim(world.client, vars(chat))
        if spec:
            moved(world.client, vars(chat), spec["id"], "running")
    moved(world.client, world.worker, first["run_id"], "verifying", "done")
    assert answer(world, chat, runs_of[first["id"]], first["id"]).status_code == 200
    change_id = one_change(world)["id"]
    other = merge(world, chat, runs_of[second["id"]], change_id)
    assert other.status_code == 403 and f"thread of proposal {second['id']}" in other.json()["message"]
    early = merge(world, chat, runs_of[first["id"]], change_id)
    assert early.status_code == 409 and "it is planned" in early.json()["message"]


def test_merge_a_change_the_judge_failed_is_told_without_the_agent_and_refused(world):  # noqa: F811
    chat, made, agent_run = built(world, files=[*CLEAN, PROTECTED], end_agent=False)
    assert moved_on(world) == {"signs": 1}
    assert moved_on(world) == {"checked": 1, "open": 1}
    ((_, _, asked),) = thread_of(world, made["id"])
    assert asked == agent_run  # no new mention
    told = said(world, made["id"])[-1]
    assert "The Judge did not pass it." in told and "not the agent's to merge" in told
    assert not told.rstrip().splitlines()[-1].startswith("@")
    refused = merge(world, chat, agent_run, one_change(world)["id"])
    assert refused.status_code == 409 and "the Judge did not pass it" in refused.json()["message"]
    assert world.github.merges == []


def test_merge_a_charter_without_answer_by_tells_no_thread_and_the_hub_leaves_it_open(world):  # noqa: F811
    from tests.hub.test_curator_changes import accepted

    accepted(world, kind="fix")
    builder(world)
    assert moved_on(world) == {"opened": 1}
    judged(world)
    world.github.ci(HEAD, runs=GREEN, statuses=SUCCESS)
    assert moved_on(world) == {"checked": 1, "open": 1}
    assert sql(world.db, select(tables.proposal_threads.c.proposal_id)) == []
    assert sql(world.db, select(tables.chats.c.id).where(tables.chats.c.title == f"Triage: {PROJECT}")) == []


def test_worker_curator_merge_sends_the_change_and_prints_one_line(monkeypatch, capsys):
    sent = []

    def call(run, route, body):
        sent.append((route, body))
        change = {"id": 4, "state": "merged", "pr_url": "https://github.com/northwind/api/pull/9", "merge_sha": "abc"}
        return {"message_id": 12, "change": change}

    monkeypatch.setattr(curator_cli, "agent_run", lambda command: SimpleNamespace(run_id=7))
    monkeypatch.setattr(curator_cli, "call", call)
    assert curator_cli.cmd_merge(SimpleNamespace(change=4)) == 0
    assert sent == [("curator/merge", {"change_id": 4})]
    assert capsys.readouterr().out.strip() == (
        "Change #4 is merged: pull request https://github.com/northwind/api/pull/9 merged as abc; the Decision line is "
        "message #12."
    )
