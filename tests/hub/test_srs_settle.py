"""The settle chat of a version (``evo_agents.hub.server.srs.settle``), as criterion a7 of plan
srs-conflicts-and-tasks names it.

When @ba's review of a version ends with a finding of level decide open, the hub opens one chat for that version, of
kind settle, with no model asked: the plugin's events contribution takes the end of the task run (a core plugin needs
no runs:dispatch for it) and the delivery opens the chat; a second delivery finds it. The hub invites by rule: the
version's author, the roles ba, qa and dev of the plugin's config, the author of the version that holds a code a
source names, the author of a plan a source names, the authors of the newest commits of a file a source names
(through their emails), and the assignees of the tasks of the use case the finding is about. Someone with no account,
no grant or a grant below the chat's label is named in the first message with why. The first message lists the
findings and mentions @ba, whose run goes to the worker of the type's run_as; every message of a person in the chat
wakes @ba without an @, a reader's too, while a group chat routes as before. @ba records who settled what, with the
link of the message, and proposes the next version as the author of the version the chat is for, whoever woke it.

The graph lookup is the hub's ``kg.unknown_refs``, replaced here by a graph of a few nodes and paths whose file has
known commit authors (``graph``); the last test builds a real graph and reads the newest authors of a file from it. The
stack is ``tests.hub.test_ba_agent``'s.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import plugin_events, tables
from evo_agents.hub.kg_graph import BuiltGraph, HubSession, UnknownRefs
from evo_agents.hub.server import kg
from evo_agents.hub.server.srs import settle
from evo_agents.kg.build import build_project
from evo_agents.kg.policy import Label
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.hub import live
from tests.hub.live import ADMIN, sql
from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
    CHI,
    DOC,
    OTHER,
    OWNER,
    READER,
    SRS,
    WRITER,
    agent_of,
    app_key,
    client,
    enable,
    enable_ba,
    granted,
    hub,
    imported,
    item,
    members,
    owners_worker,
    review,
    running,
    runs_of,
    srs_core,
    submitted,
    tool,
    version,
)
from tests.hub.test_chat_runs import created, sent
from tests.hub.test_runs import PROJECT, moved, plan_body
from tests.hub.test_srs_findings import billing
from tests.kg.projects import make_project

DAO, EVE = "dao", "eve"  # the author of plan rollout, and the assignee of a task of UC ORD-2
STRANGER_EMAIL = "lumen.dev@northwind.example"  # an author of a commit whom no member lists
FILE = "lumen-docs:docs/billing.md"
NODES = {"plan:rollout/step:1"}
SETTLE_CHATS = tables.srs.settle_chats


@pytest.fixture
def graph(monkeypatch) -> list[tuple]:
    """The project's graph on the hub holds NODES and FILE, whose newest commits are by a stranger and by dao (by his
    GitHub noreply address); each lookup's ids and paths."""
    asked = []

    async def unknown_refs(state, user, project, sink, ids, paths):
        asked.append((list(ids), list(paths)))
        authors = (STRANGER_EMAIL, f"9001+{DAO}@users.noreply.github.com") if FILE in paths else ()
        missing = tuple(path for path in paths if path != FILE)
        return UnknownRefs(tuple(node for node in ids if node not in NODES), missing, None, authors)

    monkeypatch.setattr(kg, "unknown_refs", unknown_refs)
    return asked


def deliver(client) -> dict:
    return client.portal.call(lambda: plugin_events.deliver(client.app.state.engine))


def deliveries(db) -> list[tuple]:
    d = tables.plugin_deliveries
    query = select(d.c.contribution, d.c.state, d.c.attempts).where(d.c.kind == "task.result").order_by(d.c.id)
    return sql(db, query)


def settle_rows(db) -> list[tuple]:
    sc = SETTLE_CHATS
    return sql(db, select(sc.c.chat_id, sc.c.root_message_id, sc.c.agent_run_id).order_by(sc.c.id))


def messages(client, headers, chat_id: int) -> list[dict]:
    page = client.get(f"/v1/chats/{chat_id}/messages", params={"limit": 200}, headers=headers)
    assert page.status_code == 200, page.text
    return page.json()["messages"]


def team(client, hub, github) -> dict:
    """``hub`` with dao and eve, writers who signed in, the roles of srs's config (ba owner; qa reader; dev a login
    with no account and the hub admin, who holds no grant), tasks on, plan rollout written by dao, billing written by
    someone-else and a task of UC ORD-2 assigned to eve."""
    for login, number in ((DAO, 811), (EVE, 812)):
        hub[login] = live.bearer(live.sign_in(client, github, login, number)["token"])
        granted(client, hub, login, WRITER)
    roles = {"ba": [OWNER], "qa": [READER], "dev": ["ghost", ADMIN]}
    configured = client.post(
        f"/v1/projects/{PROJECT}/plugins/srs/config", json={"values": {"roles": roles}}, headers=hub["owner"]
    )
    assert configured.status_code == 200, configured.text
    enable(client, hub["owner"], "tasks", ["plans:read"])
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/rollout", json={"body": plan_body()}, headers=hub[DAO])
    assert pushed.status_code == 200, pushed.text
    billing(client, hub["other"])
    imported(client, hub["chi"])
    body = {"title": "Retry payments", "assignees": [EVE], "links": {"use_cases": ["UC ORD-2"]}}
    task = client.post(f"/v1/x/tasks/{PROJECT}/tasks", json=body, headers=hub["chi"])
    assert task.status_code == 201, task.text
    return hub


def reviewed(client, hub, findings_of=("decide",)) -> tuple[dict, int]:
    """@ba on under owner, demo-shop v1 sent to review by chi, and @ba's review recording a finding of each level of
    ``findings_of`` before its run ends done: (owner's studio, the review run)."""
    enable_ba(client, hub)
    studio = owners_worker(client, hub, slots=6)
    submitted(client, hub["chi"], 1)
    run_id = running(client, studio, "task")["id"]
    agent = agent_of(studio, run_id)
    for level in findings_of:
        sources = [{"kind": "code", "ref": "BR BIL2-1", "note": "5 retries in 30 minutes"}]
        sources += [{"kind": "node", "ref": "plan:rollout/step:1"}, {"kind": "path", "ref": FILE}]
        arguments = {
            "kind": "contradiction",
            "level": level,
            "item": "BR ORD2-1",
            "summary": f"Retries differ ({level})",
        }
        text, error = tool(
            client, agent, "srs__finding", action="record", document="demo-shop", sources=sources, **arguments
        )
        assert not error and " is recorded: " in text, text
    moved(client, studio, run_id, "verifying", "done", summary="Found conflicts.")
    return studio, run_id


def opened_chat(client, hub, hub_db, github) -> tuple[dict, int, int]:
    """The settle chat of demo-shop v1 opened by the delivery of the end of @ba's review: (studio, chat, its first
    message)."""
    team(client, hub, github)
    studio, _ = reviewed(client, hub)
    deliver(client)
    [(chat_id, root_id, _)] = settle_rows(hub_db)
    return studio, chat_id, root_id


# Opening the chat


def test_srs_settle_chat_opens_once_when_the_review_ends_with_a_decide_finding_and_invites_by_the_rules(
    client, hub, hub_db, github, graph
):
    team(client, hub, github)
    studio, review_run = reviewed(client, hub, ("decide", "advisory"))
    assert graph == [(["plan:rollout/step:1"], [FILE])] * 2
    f = tables.srs.findings
    authors = [STRANGER_EMAIL, f"9001+{DAO}@users.noreply.github.com"]
    kept = sql(hub_db, select(f.c.level, f.c.commit_authors).order_by(f.c.id))
    assert kept == [("decide", authors), ("advisory", authors)]
    shown = client.get(f"{DOC}/findings", headers=hub["reader"]).json()
    assert all("commit_authors" not in row for row in shown), "no route returns the authors"
    assert settle_rows(hub_db) == [], "the chat opens when the event is delivered, after the run's end"

    deliver(client)
    assert deliveries(hub_db) == [("reviews", "delivered", 1)]  # srs is granted no runs:dispatch
    [(chat_id, root_id, agent_run)] = settle_rows(hub_db)
    chat = client.get(f"/v1/chats/{chat_id}", headers=hub["chi"]).json()
    assert (chat["kind"], chat["title"], chat["created_by"]) == ("settle", "Conflicts of demo-shop v1", CHI)
    assert sorted(person["login"] for person in chat["people"]) == sorted([CHI, DAO, EVE, OTHER, OWNER, READER])
    assert [agent["handle"] for agent in chat["agents"]] == ["ba"]
    [first] = messages(client, hub["chi"], chat_id)
    body = first["body"]
    assert first["id"] == root_id and first["author"]["kind"] == "hub"
    (decide,) = [row["id"] for row in shown if row["level"] == "decide"]
    assert f"- #{decide} contradiction, BR ORD2-1, found on version 1: Retries differ (decide)" in body
    assert "code BR BIL2-1 (5 retries in 30 minutes); node plan:rollout/step:1; path lumen-docs" in body
    assert "Retries differ (advisory)" not in body
    for invited in (
        f"{CHI} (the author of version 1)",
        f"{OWNER} (role ba)",
        f"{READER} (role qa)",
        f"{OTHER} (the author of version 1 of billing, which holds BR BIL2-1)",
        f"{DAO} (the author of plan rollout)",
        f"{EVE} (assigned a task of UC ORD-2)",
    ):
        assert invited in body, invited
    assert "ghost (role dev): no account on this hub" in body
    assert f"{ADMIN} (role dev): no grant on project {PROJECT}" in body
    assert f"{STRANGER_EMAIL} (an author of the newest commits of a file finding #{decide} names)" in body
    assert body.endswith("\n\n@ba") and '"Đã chốt: ..."' in body
    # @ba's first run, on a worker of run_as, owner, whom a run a message of the hub wakes acts for, at hop 1
    r = tables.runs
    run = sql(hub_db, select(r.c.chat_id, r.c.chat_message_id, r.c.hop, r.c.dispatched_by).where(r.c.id == agent_run))
    assert run[0][:3] == (chat_id, root_id, 1)
    assert [row[2] for row in runs_of(hub_db, "chat")] == [OWNER]
    # the open decide finding links the chat, and the gate names it
    link = f"/chats/{chat_id}?message={root_id}"
    assert [row["chat_link"] for row in client.get(f"{DOC}/findings", headers=hub["reader"]).json()] == [link, None]
    held = review(client, hub["other"], 1)
    assert held.status_code == 409 and f"#{decide} (contradiction, BR ORD2-1, chat {link})" in held.json()["message"]
    # a delivery again, or a call again, finds the chat it opened
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    engine = client.app.state.engine

    async def again():
        async with engine.begin() as conn:
            return await settle.open_chat(conn, project_id, review_run)

    found = client.portal.call(again)
    assert (found.chat_id, found.root_id, found.agent_run_id) == (chat_id, root_id, agent_run)
    assert len(settle_rows(hub_db)) == 1 and len(messages(client, hub["chi"], chat_id)) == 1


def test_srs_settle_chat_opens_for_no_review_without_an_open_decide_finding_nor_for_an_older_round(
    client, hub, hub_db, github, graph
):
    team(client, hub, github)
    studio, review_run = reviewed(client, hub, ("advisory",))
    deliver(client)
    assert deliveries(hub_db) == [("reviews", "delivered", 1)] and settle_rows(hub_db) == []
    # sent back and submitted again, the version's second round has a review of its own, which finds a conflict
    assert review(client, hub["other"], 1, "request_changes", body="Retries?").status_code == 200
    submitted(client, hub["chi"], 1)
    second = running(client, studio, "task")["id"]
    sources = [{"kind": "code", "ref": "BR BIL2-1"}]
    text, error = tool(
        client,
        agent_of(studio, second),
        "srs__finding",
        action="record",
        document="demo-shop",
        kind="contradiction",
        level="decide",
        summary="Retries differ",
        sources=sources,
    )
    assert not error, text
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]

    async def opening(run_id: int):
        async with client.app.state.engine.begin() as conn:
            return await settle.open_chat(conn, project_id, run_id)

    assert client.portal.call(opening, review_run) is None, "the first round's review is no longer the version's"
    moved(client, studio, second, "verifying", "done", summary="One conflict.")
    deliver(client)
    [(chat_id, root_id, _)] = settle_rows(hub_db)
    assert client.portal.call(opening, second).chat_id == chat_id


def test_srs_settle_a_failure_opening_the_chat_leaves_the_run_ended_and_the_delivery_tried_again(
    client, hub, hub_db, github, graph, monkeypatch
):
    team(client, hub, github)

    async def broken(conn, project_id, review_run_id):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(settle, "open_chat", broken)
    _, review_run = reviewed(client, hub)
    assert deliver(client)["retried"] == 1
    assert deliveries(hub_db) == [("reviews", "pending", 1)]
    r = tables.runs
    assert sql(hub_db, select(r.c.state).where(r.c.id == review_run)) == [("done",)]
    assert settle_rows(hub_db) == []


# In the chat


def test_srs_settle_chat_wakes_ba_at_every_message_of_a_member_a_readers_too_while_a_group_chat_routes_as_before(
    client, hub, hub_db, github, graph
):
    _, chat_id, root_id = opened_chat(client, hub, hub_db, github)
    [(_, _, first_run)] = settle_rows(hub_db)
    said = sent(client, hub["reader"], chat_id, "Billing allows 5 retries; keep 3 here?")
    [(name, via, run_id, refusal)] = [(m["name"], m["via"], m["run_id"], m["refusal"]) for m in said["mentions"]]
    assert (name, via, refusal) == ("ba", "settle", None) and run_id not in (None, first_run)
    answered = sent(client, hub["chi"], chat_id, "Keep 3, billing is another product.", reply_to=root_id)
    [(via, chis)] = [(m["via"], m["run_id"]) for m in answered["mentions"]]
    assert via == "settle" and chis not in (None, first_run, run_id)  # a session of chi's in the first thread
    again = sent(client, hub["chi"], chat_id, "And the notice rule stays.", reply_to=root_id)
    assert [(m["via"], m["run_id"]) for m in again["mentions"]] == [("settle", chis)]  # in its inbox
    mentioned = sent(client, hub["other"], chat_id, "@ba what does billing say exactly?")
    assert [(m["via"], m["refusal"]) for m in mentioned["mentions"]] == [("handle", None)]  # one run, not two
    # a run of the reader's: dispatched by run_as, owner, for the reader
    r = tables.runs
    assert sql(hub_db, select(r.c.dispatched_by).where(r.c.id == run_id)) == sql(
        hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER)
    )
    # a group chat with @ba routes as before: no run without an @, and a reader's @ba is refused
    group = created(client, hub["other"], "SRS", people=[READER], agents=("ba",))
    assert sent(client, hub["other"], group["id"], "what about retries?")["mentions"] == []
    refused = sent(client, hub["reader"], group["id"], "@ba what about retries?")["mentions"]
    assert refused[0]["run_id"] is None and "works for the writers of project" in refused[0]["refusal"]


def test_srs_settle_ba_records_the_settling_and_proposes_the_next_version_as_the_author(
    client, hub, hub_db, github, graph
):
    studio, chat_id, root_id = opened_chat(client, hub, hub_db, github)
    said = sent(client, hub["reader"], chat_id, "Keep 3 retries, billing is another product.")
    chis_run = running(client, studio, "chat")
    readers = running(client, studio, "chat")
    assert readers["id"] == said["mentions"][0]["run_id"] != chis_run["id"]
    agent = agent_of(studio, readers["id"])
    (decide,) = [row["id"] for row in client.get(f"{DOC}/findings", headers=hub["chi"]).json()]
    link = f"/chats/{chat_id}?message={said['id']}"
    text, error = tool(
        client,
        agent,
        "srs__finding",
        action="resolve",
        document="demo-shop",
        finding=decide,
        resolution="Keep 3 retries: billing is another product.",
        settled_by=READER,
        chat_link=link,
    )
    assert not error and f"Resolved by {READER}: Keep 3 retries" in text, text
    settled = client.get(f"{DOC}/findings", headers=hub["chi"]).json()[0]
    assert (settled["status"], settled["settled_by"], settled["chat_link"]) == ("resolved", READER, link)
    retries = item(client, hub["chi"], "BR ORD2-1")
    body = retries["body"] + "\n- Billing's 5 retries are another product's."
    text, error = tool(
        client,
        agent,
        "srs__propose",
        document="demo-shop",
        base_version=1,
        summary="Keep 3 retries, as settled in the chat",
        changes=[{"key": retries["key"], "body": body}],
    )
    assert not error and f"written as {CHI}, who therefore does not approve it" in text, text
    proposed = version(client, hub["reader"], 2)
    assert (proposed["state"], proposed["author"], proposed["drafted_by_run"]) == ("in_review", CHI, readers["id"])
    # an objection within 24 hours reopens it, and it holds approval again
    text, error = tool(client, agent, "srs__finding", action="reopen", document="demo-shop", finding=decide)
    assert not error and "is open again" in text, text
    assert client.get(f"{DOC}/findings", headers=hub["chi"]).json()[0]["chat_link"] == link


def test_srs_settle_proposal_outside_a_settle_chat_stays_the_mentioners(client, hub, hub_db, github, graph):
    studio, chat_id, _ = opened_chat(client, hub, hub_db, github)
    group = created(client, hub["other"], "SRS", people=[OWNER], agents=("ba",))
    sent(client, hub["other"], group["id"], "@ba add a line about billing")
    running(client, studio, "chat")  # the first run of the settle chat, for chi
    spec = running(client, studio, "chat")
    retries = item(client, hub["chi"], "BR ORD2-1")
    text, error = tool(
        client,
        agent_of(studio, spec["id"]),
        "srs__propose",
        document="demo-shop",
        base_version=1,
        summary="A line",
        changes=[{"key": retries["key"], "body": retries["body"] + "\n- One more line."}],
    )
    assert not error and f"written as {OTHER}" in text, text


# The authors of a file's newest commits, from a built graph


def _commit_as(repo: Path, email: str, when: str, text: str) -> None:
    (repo / "docs/guide.md").write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": when}
    env |= {"GIT_COMMITTER_NAME": "c", "GIT_COMMITTER_EMAIL": "c@example.test", "GIT_COMMITTER_DATE": when}
    for args in (["add", "-A"], ["commit", "-q", "-m", "edit"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def test_srs_settle_graph_gives_the_authors_of_the_three_newest_commits_of_a_file(tmp_path, monkeypatch):
    home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    project = make_project(tmp_path, home)
    app = tmp_path / "demo-app"
    # three newer than the first commit, in other time zones: by UTC nam's, then lan's (04:00), then mai's (00:00),
    # where the text of their local times would put mai's before lan's
    _commit_as(app, "nam@lumen.example", "2026-10-05T08:00:00+00:00", "# Guide\n\nOne.\n")
    _commit_as(app, "lan@lumen.example", "2026-10-04T23:00:00-05:00", "# Guide\n\nTwo.\n")
    _commit_as(app, "mai@lumen.example", "2026-10-05T07:00:00+07:00", "# Guide\n\nThree.\n")
    assert sync_project(project, ["app"])[0].ok
    report = build_project(project)
    assert report.ok, report.errors
    store = Store.for_project(project)
    built = BuiltGraph(report.build_id, "0" * 64, 0, "c", 0, 0, datetime.now(UTC))
    wide = Label(2, 0, "U", frozenset({project.name}))
    session = HubSession(project.name, project.policy, wide, "wide", store, built, built)
    try:
        lacking, missing, authors = session.unknown(["app:file:docs/guide.md", "app:file:none.md"], ["app:app/x.py"])
        assert (lacking, missing) == (["app:file:none.md"], ["app:app/x.py"])
        assert authors == ["nam@lumen.example", "lan@lumen.example", "mai@lumen.example"]  # not t@, the first's
        assert session.unknown([], ["docs/guide.md"])[2] == authors
        assert session.unknown([], ["app:app/search.py"])[2] == ["t@example.test"]
        node = store.node([c for c in store.alive_ids("nodes", report.build_id) if ":commit:" in c][0], report.build_id)
        assert node["props"]["time"].endswith("Z")
    finally:
        store.close()
