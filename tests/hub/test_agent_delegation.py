"""An agent of a chat run asks an agent of another project (``evo-agents worker agent ask``,
``evo_agents.hub.server.agent_asks``), as criterion b4 of the plan chat-first-home names it.

b4: the ask makes a run of the agent asked, on its pool, for the person at the origin of the chain, one hop further,
linked to the asking run by ``spawned_by_run_id``; the asking run parks on a wait and holds no worker, and the end of
the run asked wakes it with the answer, or with why it failed; the thread of the asking run shows what was asked and
the answer. The hub refuses, with why and with no run, when that person may not give the agent work, when the agent
has no pool, and past MAX_HOPS.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, func, insert, select, update

from evo_agents.hub import chat_runs, tables
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401
    CHECKOUTS,
    OTHER,
    OWNER,
    app_key,
    chat_worker,
    claim,
    client,
    created,
    ended,
    hub,
    in_chats,
    moved,
    recover,
    reply,
    started,
    thread,
)
from tests.hub.test_chats import agent
from tests.hub.test_plans import registration

KB = "lumen-kb"
KB_AGENT = "kb-bot"
KB_CHECKOUTS = {**CHECKOUTS, f"{KB}/evo-agents": {"path": "/src/lumen-kb", "branch": "main"}}
QUESTION = "Which service of lumen-kb signs the export links, and where is its key read?"


def kb_project(client, hub, db, role: str | None = "writer", login: str = OTHER) -> None:
    """Project lumen-kb with the agent kb-bot, which has a pool and an account pool; the owner is its admin, and
    ``login`` holds ``role`` there (no grant for None)."""
    assert client.put(f"/v1/projects/{KB}", json=registration(), headers=hub["admin"]).status_code == 200
    admin = {"role": "admin", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{KB}/grants/{OWNER}", json=admin, headers=hub["admin"]).status_code == 200
    if role is not None:
        grant = {"role": role, "max_level": "internal"}
        response = client.put(f"/v1/admin/projects/{KB}/grants/{login}", json=grant, headers=hub["admin"])
        assert response.status_code == 200, response.text
    agent(client, hub["owner"], KB_AGENT, project=KB)
    give_account_pool(db, KB, KB_AGENT, OWNER)


def kb_node(client, headers, db, name: str = "kb-node") -> dict:
    """A worker of ``headers``' member in the pool of kb-bot, serving lumen-kb."""
    worker = chat_worker(client, headers, name, checkouts=KB_CHECKOUTS, pool=KB_AGENT)
    (pid,) = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == KB))[0]
    sql(db, insert(tables.worker_projects).values(worker_id=worker["id"], project_id=pid))
    return worker


def ask(client, worker: dict, run_id: int, handle: str = KB_AGENT, text: str = QUESTION) -> dict:
    response = in_chats(client, worker, run_id, "agent/asks", {"handle": handle, "body": text})
    assert response.status_code == 201, response.text
    return response.json()


def runs_of(db) -> list[tuple]:
    r = tables.runs
    query = select(r.c.id, r.c.state, r.c.hop, r.c.spawned_by_run_id, r.c.resume_of_run_id).order_by(r.c.id)
    return sql(db, query.where(r.c.kind == "chat"))


def bodies(client, headers, chat: dict, root: int) -> list[str]:
    return [item["body"] for item in thread(client, headers, chat["id"], root)]


def test_delegation_parks_and_wakes_parent(client, hub, hub_db):
    kb_project(client, hub, hub_db)
    studio = chat_worker(client, hub["owner"], "studio")
    node = kb_node(client, hub["owner"], hub_db)
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, studio, chat)
    parent = spec["id"]

    asked = ask(client, studio, parent)
    assert asked["refusal"] is None and asked["run_id"] is not None and asked["decision_id"] is not None
    child = asked["run_id"]
    r, d = tables.runs, tables.decisions
    (row,) = sql(
        hub_db,
        select(r.c.hop, r.c.spawned_by_run_id, r.c.spawned_wait_id, r.c.project_id, r.c.state).where(r.c.id == child),
    )
    (kb_id,) = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == KB))[0]
    assert row == (2, parent, asked["decision_id"], kb_id, "queued")
    view = client.get(f"/v1/projects/{KB}/runs/{child}", headers=hub["owner"]).json()
    assert (view["dispatched_by"], view["profile"], view["spawned_by_run_id"]) == (OWNER, KB_AGENT, parent)
    wait = sql(hub_db, select(d.c.run_id, d.c.category, d.c.state).where(d.c.id == asked["decision_id"]))
    assert wait == [(parent, "wait", "open")]
    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert f"@reviewer asked @{KB_AGENT} of project {KB} (run #{child}): {QUESTION}" in shown

    # the agent ends its turn: the run waits only for the wait, so the reaper parks it and it holds no worker
    moved(client, studio, parent, "waiting")
    recover(client)
    assert sql(hub_db, select(r.c.state, r.c.worker_id).where(r.c.id == parent)) == [("parked", studio["id"])]

    taken = claim(client, node)
    assert taken is not None and taken["id"] == child
    moved(client, node, child, "running", session_id="sess-kb")
    reply(client, node, child, "The export service signs them; it reads the key in kb/export/keys.py::load_key.")
    ended(client, node, child)

    assert sql(hub_db, select(d.c.state).where(d.c.id == asked["decision_id"])) == [("answered",)]
    (answer,) = sql(hub_db, select(d.c.answer_text).where(d.c.id == asked["decision_id"]))[0]
    assert f"run #{child} of @{KB_AGENT}, which you asked, answered" in answer and "load_key" in answer
    resumed = sql(hub_db, select(r.c.id, r.c.state, r.c.pinned_worker_id).where(r.c.resume_of_run_id == parent))
    assert [(state, pinned) for _, state, pinned in resumed] == [("queued", studio["id"])]
    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert any(item.startswith(f"@{KB_AGENT} answered (run #{child}):") and "load_key" in item for item in shown)

    # a run asked that fails wakes its asker with why
    (new_id,) = [run_id for run_id, *_ in resumed]
    assert claim(client, studio)["id"] == new_id
    moved(client, studio, new_id, "running", session_id="sess-1")
    again = ask(client, studio, new_id, text="And who rotates that key?")
    assert again["refusal"] is None
    assert claim(client, node)["id"] == again["run_id"]
    moved(client, node, again["run_id"], "running", session_id="sess-kb-2")
    moved(client, node, again["run_id"], "failed", error="the export repo has no checkout here")
    (answer,) = sql(hub_db, select(d.c.answer_text).where(d.c.id == again["decision_id"]))[0]
    assert f"run #{again['run_id']} of @{KB_AGENT}, which you asked, ended failed" in answer
    assert "the export repo has no checkout here" in answer
    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert any(item.startswith(f"@{KB_AGENT} gave no answer: run #{again['run_id']} ended failed") for item in shown)


def test_an_agent_naming_the_agent_it_asked_is_refused_nothing(client, hub, hub_db):
    """The asking run's own messages that name @kb-bot, before it parks and after the answer resumes it, point at the
    run asked rather than saying @kb-bot is not in the chat; an agent it did not ask is still refused."""
    from evo_agents.hub.server.chats import NOT_IN_CHAT

    kb_project(client, hub, hub_db)
    agent(client, hub["owner"], "kb-docs", project=KB)
    studio = chat_worker(client, hub["owner"], "studio")
    node = kb_node(client, hub["owner"], hub_db)
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, studio, chat)
    child = ask(client, studio, spec["id"])["run_id"]

    def mentions_of(text: str) -> list[tuple]:
        (item,) = [item for item in thread(client, hub["owner"], chat["id"], mention["id"]) if item["body"] == text]
        return [(m["name"], m["run_id"], m["refusal"]) for m in item["mentions"]]

    waiting = f"I asked @{KB_AGENT}; @kb-docs may know too."
    reply(client, studio, spec["id"], waiting)
    refused = NOT_IN_CHAT.format(name="kb-docs")
    assert mentions_of(waiting) == [(KB_AGENT, child, None), ("kb-docs", None, refused)]

    moved(client, studio, spec["id"], "waiting")
    recover(client)
    assert claim(client, node)["id"] == child
    moved(client, node, child, "running", session_id="sess-kb")
    reply(client, node, child, "The export service signs them.")
    ended(client, node, child)
    r = tables.runs
    ((resumed,),) = sql(hub_db, select(r.c.id).where(r.c.resume_of_run_id == spec["id"]))
    assert claim(client, studio)["id"] == resumed
    moved(client, studio, resumed, "running", session_id="sess-1")
    answered = f"@{KB_AGENT} says the export service signs them."
    reply(client, studio, resumed, answered)
    assert mentions_of(answered) == [(KB_AGENT, child, None)]
    assert len(runs_of(hub_db)) == 3  # naming it handed @kb-bot no new work


@pytest.mark.parametrize(
    ("role", "said"),
    [
        (None, f"no agent @{KB_AGENT} that {OTHER} can see"),
        ("reader", f"@{KB_AGENT} works for the writers of project {KB}, and {OTHER} holds reader there"),
    ],
)
def test_delegation_refused_without_right_to_task_target(client, hub, hub_db, role, said):
    """The origin of the chain is someone-else, a writer of evo-agents who may not give kb-bot work; the owner, admin
    of both projects and owner of the pools, could, and does not count."""
    kb_project(client, hub, hub_db, role=role)
    theirs = chat_worker(client, hub["other"], "theirs")
    kb_node(client, hub["other"], hub_db)
    chat = created(client, hub["other"])
    mention, spec = started_by(client, hub["other"], theirs, chat)
    before = runs_of(hub_db)

    refused = ask(client, theirs, spec["id"])
    assert refused == {"run_id": None, "decision_id": None, "refusal": said}
    assert runs_of(hub_db) == before
    d = tables.decisions
    assert sql(hub_db, select(func.count()).select_from(d).where(d.c.run_id == spec["id"])) == [(0,)]
    assert f"@{KB_AGENT}: {said}" in bodies(client, hub["other"], chat, mention["id"])


def test_delegation_hop_limit(client, hub, hub_db):
    assert chat_runs.MAX_HOPS == 3
    kb_project(client, hub, hub_db)
    studio = chat_worker(client, hub["owner"], "studio")
    kb_node(client, hub["owner"], hub_db)
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, studio, chat)
    r = tables.runs
    sql(hub_db, update(r).values(hop=chat_runs.MAX_HOPS).where(r.c.id == spec["id"]))
    before = runs_of(hub_db)

    refused = ask(client, studio, spec["id"])
    assert refused["run_id"] is None and refused["decision_id"] is None
    assert refused["refusal"] == chat_runs.hop_problem(chat_runs.MAX_HOPS + 1, False)
    assert "at most 3 hops" in refused["refusal"]
    assert runs_of(hub_db) == before

    # one hop less is taken, and an agent with no pool of its own is refused with why
    sql(hub_db, update(r).values(hop=chat_runs.MAX_HOPS - 1).where(r.c.id == spec["id"]))
    agent(client, hub["owner"], "kb-poolless", project=KB)
    p = tables.worker_pools
    profile = select(tables.agent_profiles.c.id).where(tables.agent_profiles.c.handle == "kb-poolless")
    sql(hub_db, delete(p).where(p.c.agent_id == profile.scalar_subquery()))
    no_pool = ask(client, studio, spec["id"], handle="kb-poolless")
    assert no_pool["run_id"] is None
    assert no_pool["refusal"].startswith("@kb-poolless has no pool of its own")
    assert [run_id for run_id, *_ in runs_of(hub_db)] == [run_id for run_id, *_ in before]
    taken = ask(client, studio, spec["id"])
    assert taken["refusal"] is None
    (row,) = sql(hub_db, select(r.c.hop, r.c.spawned_by_run_id).where(r.c.id == taken["run_id"]))
    assert row == (chat_runs.MAX_HOPS, spec["id"])
    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert f"@{KB_AGENT}: {refused['refusal']}" in shown


def started_by(client, headers, worker: dict, chat: dict) -> tuple:
    """``started`` for the member of ``headers``: their mention of @reviewer, its run claimed by ``worker``."""
    from tests.hub.test_chat_runs import sent

    mention = sent(client, headers, chat["id"], "@reviewer where do the export links come from?")
    spec = claim(client, worker)
    assert spec is not None and spec["kind"] == "chat", spec
    moved(client, worker, spec["id"], "running", session_id="sess-2")
    return mention, spec
