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
helpers are ``tests.hub.test_curator_changes``'s."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.collect import reopen_deferred
from evo_agents.hub.server.curator_threads import SKILL
from evo_agents.worker import curator_cli
from tests.hub.live import sql
from tests.hub.test_brief import BRIEF, body_of, briefed, briefs_of
from tests.hub.test_chat_runs import chat_worker, thread
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import grant, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    curator_charter,
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
