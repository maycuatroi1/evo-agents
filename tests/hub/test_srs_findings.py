"""The conflicts @ba finds in a BA document, and the gate of approval (``evo_agents.core_plugins.srs.findings``), as
criteria a4 and a5 of plan srs-conflicts-and-tasks name them.

The agent of a run of @ba records a finding with srs__finding, and only with sources the hub looks up first: a code an
item of the project holds (here one of a second document), a node id or a path of the project's knowledge graph on the
hub as the run reads it. A finding without a source, or with one the hub cannot find, writes nothing. The agent
resolves (by a member in chat, or from the graph by sources), waives and reopens findings, the last at most 24 hours
after they were settled. Readers read the findings; an admin of the project waives an open one, and another writer or
the agent of a run gets 403. A version is not approved while its document has an open finding of level decide, whose
id and chat link the 409 names, nor while @ba's review of its round is queued or running; info and advisory findings,
a review run that failed or was cancelled, a round without one, and a request for changes are never held.

The graph lookup is the hub's ``kg.unknown_refs``: one test reads it as it is on a project without a graph, the others
replace it with a graph of a few nodes and paths (``graph``); ``tests.hub.test_srs_tasks_plugin_kg`` reads a built one.
The stack is ``tests.hub.test_ba_agent``'s.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from evo_agents.hub.kg_graph import UnknownRefs
from evo_agents.hub.server import kg
from tests.hub.live import ADMIN, sql
from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
    CHI,
    DOC,
    OTHER,
    OWNER,
    SAMPLE,
    SRS,
    TYPE,
    agent_of,
    app_key,
    client,
    enable_ba,
    hub,
    imported,
    item,
    members,
    mention,
    owners_worker,
    review,
    running,
    saved,
    srs_core,
    submitted,
    tool,
)
from tests.hub.test_chat_runs import created
from tests.hub.test_runs import PROJECT, control, moved

NODES = {"usecase:UC BIL-1", "plan:rollout/step:1"}
PATHS = {"lumen-docs:docs/billing.md"}
FINDINGS = f"{DOC}/findings"
CHAT = f"/p/{PROJECT}/chats/7#m-12"


@pytest.fixture
def graph(monkeypatch) -> list[tuple]:
    """The project's graph on the hub holds NODES and PATHS (a path alone names one of them too); each lookup's
    project, ids and paths."""
    asked = []

    async def unknown_refs(state, user, project, sink, ids, paths):
        asked.append((project, list(ids), list(paths)))
        bare = {path.partition(":")[2] for path in PATHS}
        missing = tuple(path for path in paths if path not in PATHS and path not in bare)
        return UnknownRefs(tuple(node for node in ids if node not in NODES), missing)

    monkeypatch.setattr(kg, "unknown_refs", unknown_refs)
    return asked


def billing(client, headers) -> None:
    """A second document of the project, billing, whose codes are BIL and RFD where demo-shop's are ORD and RET."""
    text = SAMPLE.read_text(encoding="utf-8").replace("ORD", "BIL").replace("RET", "RFD")
    for old, new in (("CBR 1", "CBR 3"), ("CBR 2", "CBR 4"), ("SEC-01", "SEC-02"), ("PERF-01", "PERF-02")):
        text = text.replace(old, new)
    answer = client.post(f"{SRS}/import", json={"markdown": text, "slug": "billing"}, headers=headers)
    assert answer.status_code == 200, answer.text


def reviewing(client, hub) -> tuple[dict, int, dict]:
    """@ba on, demo-shop imported by chi and sent to review, @ba's review run running on owner's studio: (the
    studio, the run, the agent's headers)."""
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    run_id = running(client, studio, "task")["id"]
    return studio, run_id, agent_of(studio, run_id)


def recorded(client, agent, **arguments) -> int:
    """The id of the finding the agent records; a contradiction of level decide about BR ORD2-1 unless told."""
    found = {"kind": "contradiction", "level": "decide", "item": "BR ORD2-1", "summary": "Retries differ"} | arguments
    found.setdefault("sources", [{"kind": "code", "ref": "BR BIL2-1", "note": "5 retries in 30 minutes"}])
    found = {name: value for name, value in found.items() if value is not None}
    text, error = tool(client, agent, "srs__finding", action="record", document="demo-shop", **found)
    assert not error and " is recorded: " in text, text
    return int(text.split()[1])


def ba_in_chat(client, hub, studio: dict) -> dict:
    """The headers of the agent of a chat run of @ba, which someone-else mentioned and owner's studio runs."""
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    mention(client, hub["other"], chat["id"], "@ba settle the retries with us")
    return agent_of(studio, running(client, studio, "chat")["id"])


def findings(client, headers, status: str | None = None) -> list[dict]:
    """The findings of demo-shop, each source without the note it does not have."""
    listed = client.get(FINDINGS, params={"status": status} if status else None, headers=headers)
    assert listed.status_code == 200, listed.text
    found = listed.json()
    for row in found:
        for name in ("sources", "resolution_sources"):
            row[name] = [{key: value for key, value in source.items() if value is not None} for source in row[name]]
    return found


def settle(client, agent, action: str, finding: int, **arguments) -> tuple[str, bool]:
    return tool(client, agent, "srs__finding", action=action, document="demo-shop", finding=finding, **arguments)


# Recording a finding


def test_srs_finding_is_recorded_by_ba_only_with_sources_the_hub_finds(client, hub, hub_db, graph):
    billing(client, hub["chi"])
    studio, run_id, agent = reviewing(client, hub)
    record = {"action": "record", "document": "demo-shop", "kind": "contradiction", "level": "decide"}
    record |= {"item": "BR ORD2-1", "summary": "Demo-shop allows 3 retries in 10 minutes, billing 5 in 30."}
    text, error = tool(client, agent, "srs__finding", **record, sources=[])
    assert error and text.startswith("error: bad arguments for srs__finding"), "no source, no conflict"
    text, error = tool(client, agent, "srs__finding", **record)
    assert error and "srs__finding record needs sources" in text, text
    for source, said in (
        ({"kind": "code", "ref": "UC BIL-9"}, f"no item of project {PROJECT} holds code UC BIL-9"),
        ({"kind": "node", "ref": "usecase:UC BIL-9"}, "has id usecase:UC BIL-9"),
        ({"kind": "path", "ref": "docs/refunds.md"}, "carries path docs/refunds.md"),
        ({"kind": "version", "ref": "1"}, "one of node, code, path, not version"),
    ):
        text, error = tool(
            client, agent, "srs__finding", **record, sources=[{"kind": "code", "ref": "UC BIL-1"}, source]
        )
        assert error and said in text, text
    assert findings(client, hub["reader"]) == []
    sources = [
        {"kind": "code", "ref": "BR BIL2-1", "note": "billing retries 5 times in 30 minutes"},
        {"kind": "node", "ref": "usecase:UC BIL-1"},
        {"kind": "path", "ref": "docs/billing.md"},
    ]
    text, error = tool(client, agent, "srs__finding", **record, sources=sources)
    assert not error, text
    (found,) = findings(client, hub["reader"])
    assert text.startswith(
        f"Finding {found['id']} of demo-shop is recorded: decide, contradiction, on version 1, about item BR ORD2-1."
    ), text
    assert "While it is open, no version of the document is approved." in text
    retries = item(client, hub["chi"], "BR ORD2-1")
    assert (found["version"], found["item"], found["item_code"], found["status"]) == (
        1,
        retries["key"],
        "BR ORD2-1",
        "open",
    )
    assert (found["sources"], found["run_id"], found["settled_by"], found["resolution"]) == (
        sources,
        run_id,
        None,
        None,
    )
    assert graph[-1] == (PROJECT, ["usecase:UC BIL-1"], ["docs/billing.md"])
    f, d = tables.srs.findings, tables.srs.documents
    assert sql(hub_db, select(f.c.label)) == sql(hub_db, select(d.c.label).where(d.c.slug == "demo-shop"))
    # the whole document, on the version given
    whole = recorded(client, agent, item=None, level="info", kind="overlap", version=1)
    assert findings(client, hub["reader"])[-1]["item"] is None and whole == found["id"] + 1
    # another agent's run, and a member's own session, record nothing
    chat = created(client, hub["owner"], "SRS", people=[OTHER], agents=("ba", "reviewer"))
    mention(client, hub["owner"], chat["id"], "@reviewer check UC ORD-2")
    reviewer = agent_of(studio, running(client, studio, "chat")["id"])
    text, error = tool(client, reviewer, "srs__finding", **record, sources=sources)
    assert error and "only the agent of a chat run or a task run of @ba" in text, text
    text, error = tool(client, hub["other"], "srs__finding", PROJECT, **record, sources=sources)
    assert error and "only the agent of a chat run or a task run of @ba" in text, text
    assert len(findings(client, hub["reader"])) == 2


def test_srs_finding_with_a_node_or_a_path_is_refused_while_the_project_has_no_graph(client, hub):
    _, _, agent = reviewing(client, hub)
    for source in ({"kind": "node", "ref": "usecase:UC ORD-1"}, {"kind": "path", "ref": "docs/ordering.md"}):
        text, error = tool(
            client,
            agent,
            "srs__finding",
            action="record",
            document="demo-shop",
            kind="kg_conflict",
            level="advisory",
            summary="The graph says otherwise",
            sources=[source],
        )
        assert error and f"project {PROJECT} has no knowledge graph on the hub yet" in text, text
    assert findings(client, hub["reader"]) == []


# Settling a finding


def test_srs_finding_is_resolved_waived_and_reopened_by_ba_within_24_hours(client, hub, hub_db, graph):
    billing(client, hub["chi"])
    _, _, agent = reviewing(client, hub)
    first, second = recorded(client, agent), recorded(client, agent, level="advisory", kind="duplicate")
    text, error = settle(client, agent, "resolve", first, resolution="Three retries")
    assert error and "settled from the graph: name the sources that settle it" in text, text
    for login, said in (("nobody", "no member nobody on the hub"), (ADMIN, f"{ADMIN} holds no grant")):
        text, error = settle(client, agent, "resolve", first, resolution="Three retries", settled_by=login)
        assert error and said in text, text
    text, error = settle(client, agent, "resolve", first, resolution="x", settled_by=OTHER, chat_link="javascript:x")
    assert error and "chat_link is a path of the hub's web or an http(s) URL" in text, text
    text, error = settle(client, agent, "reopen", first, resolution="no")
    assert error and "srs__finding reopen takes no resolution" in text, text
    assert [f["status"] for f in findings(client, hub["reader"])] == ["open", "open"]
    # by a member in chat
    text, error = settle(
        client, agent, "resolve", first, resolution="Three retries, as billing", settled_by=OTHER, chat_link=CHAT
    )
    assert not error and f"Resolved by {OTHER}: Three retries, as billing" in text and f"Chat: {CHAT}" in text, text
    done = findings(client, hub["reader"])[0]
    assert (done["status"], done["settled_by"], done["chat_link"], done["resolution"]) == (
        "resolved",
        OTHER,
        CHAT,
        "Three retries, as billing",
    )
    assert done["settled_at"] is not None
    text, error = settle(client, agent, "resolve", first, resolution="again", settled_by=CHI)
    assert error and f"finding {first} of demo-shop is resolved: only an open finding is settled" in text, text
    # reopened, it keeps its chat and nothing else of the settling
    text, error = settle(client, agent, "reopen", first)
    assert not error and f"Finding {first} of demo-shop is open again" in text, text
    again = findings(client, hub["reader"])[0]
    assert (again["status"], again["settled_by"], again["settled_at"], again["resolution"]) == (
        "open",
        None,
        None,
        None,
    )
    assert again["chat_link"] == CHAT
    # from the graph: a later version of the document, or a node
    retries = item(client, hub["chi"], "BR ORD2-1")
    saved(client, hub["other"], 1, {"key": retries["key"], "body": "A failed payment can be retried 5 times."})
    text, error = settle(
        client, agent, "resolve", first, resolution="v2 says 5", sources=[{"kind": "version", "ref": "1"}]
    )
    assert error and "demo-shop has no version 1 after version 1" in text, text
    sources = [{"kind": "version", "ref": "2"}, {"kind": "node", "ref": "plan:rollout/step:1"}]
    text, error = settle(client, agent, "resolve", first, resolution="Version 2 says 5 retries", sources=sources)
    assert not error and "Resolved from the graph: Version 2 says 5 retries" in text, text
    assert "Settled by: version 2; node plan:rollout/step:1" in text, text
    settled = findings(client, hub["reader"])[0]
    assert (settled["settled_by"], settled["resolution_sources"]) == (None, sources)
    # waived by @ba, then too old to reopen
    text, error = settle(client, agent, "waive", second, resolution="Not a conflict: two names of one rule")
    assert not error and "Waived: Not a conflict: two names of one rule" in text, text
    f = tables.srs.findings
    sql(hub_db, update(f).where(f.c.id == second).values(settled_at=func.now() - timedelta(hours=25)))
    text, error = settle(client, agent, "reopen", second)
    assert error and "more than 24 hours ago and is not reopened: record a new finding" in text, text
    assert [(f["id"], f["status"]) for f in findings(client, hub["reader"])] == [
        (first, "resolved"),
        (second, "waived"),
    ]


def test_srs_findings_are_read_by_readers_and_only_an_admin_waives_an_open_one(client, hub, graph):
    billing(client, hub["chi"])
    _, run_id, agent = reviewing(client, hub)
    first = recorded(client, agent)
    assert [f["id"] for f in findings(client, hub["reader"], "open")] == [first]
    assert findings(client, hub["reader"], "waived") == []
    assert client.get(FINDINGS, params={"status": "gone"}, headers=hub["reader"]).status_code == 422
    waive = f"{FINDINGS}/{first}/waive"
    for headers in (hub["other"], hub["reader"], agent):
        refused = client.post(waive, json={"reason": "@ba cannot run"}, headers=headers)
        assert refused.status_code in (401, 403), refused.text
    waived = client.post(waive, json={"reason": "@ba has no worker this week"}, headers=hub["owner"])
    assert waived.status_code == 200, waived.text
    body = waived.json()
    assert (body["status"], body["settled_by"], body["resolution"]) == ("waived", OWNER, "@ba has no worker this week")
    again = client.post(waive, json={"reason": "again"}, headers=hub["owner"])
    assert again.status_code == 409 and "only an open finding is settled" in again.json()["message"], again.text
    missing = client.post(f"{FINDINGS}/999999/waive", json={"reason": "x"}, headers=hub["owner"])
    assert missing.status_code == 404, missing.text
    # an archived document takes no finding, and no waiver
    second = recorded(client, agent, level="advisory")
    assert client.post(f"{DOC}/archive", headers=hub["owner"]).status_code == 200
    text, error = tool(
        client,
        agent,
        "srs__finding",
        action="record",
        document="demo-shop",
        kind="overlap",
        level="info",
        summary="x",
        sources=[{"kind": "code", "ref": "UC BIL-1"}],
    )
    assert error and "is archived and takes no change" in text, text
    archived = client.post(f"{FINDINGS}/{second}/waive", json={"reason": "x"}, headers=hub["owner"])
    assert archived.status_code == 409 and "is archived" in archived.json()["message"], archived.text
    assert [f["status"] for f in findings(client, hub["reader"])] == ["waived", "open"]
    assert all(f["run_id"] == run_id for f in findings(client, hub["reader"]))


# The gate of approval


def test_srs_approval_gate_holds_a_version_while_a_decide_finding_is_open_and_names_each(client, hub, graph):
    billing(client, hub["chi"])
    studio, run_id, agent = reviewing(client, hub)
    info = recorded(client, agent, level="info", kind="overlap")
    advisory = recorded(client, agent, level="advisory", kind="duplicate")
    decide = recorded(client, agent, item="UC ORD-2")
    moved(client, studio, run_id, "verifying", "done", summary="Three findings.")
    held = review(client, hub["other"], 1)
    assert held.status_code == 409, held.text
    message = held.json()["message"]
    assert f"1 finding(s) of level decide are open: #{decide} (contradiction, UC ORD-2)" in message, message
    assert f"#{info} " not in message and f"#{advisory} " not in message
    assert f"`evo-agents hub srs waive demo-shop FINDING --project {PROJECT} --reason TEXT`" in message
    assert [entry["finding"] for entry in held.json()["detail"]] == [decide]
    # a request for changes is never held, and the finding follows the document into the next round
    returned = review(client, hub["other"], 1, "request_changes", body="Settle the retries first")
    assert returned.status_code == 200 and returned.json()["state"] == "draft", returned.text
    submitted(client, hub["chi"], 1)
    moved(client, studio, running(client, studio, "task")["id"], "verifying", "done", summary="Round 2, the same.")
    assert review(client, hub["other"], 1).status_code == 409
    # settled in chat and reopened, the 409 names the chat
    chat = ba_in_chat(client, hub, studio)
    settle(client, chat, "resolve", decide, resolution="Three retries", settled_by=CHI, chat_link=CHAT)
    settle(client, chat, "reopen", decide)
    held = review(client, hub["other"], 1)
    assert held.status_code == 409, held.text
    assert f"#{decide} (contradiction, UC ORD-2, settled in {CHAT})" in held.json()["message"], held.text
    assert held.json()["detail"][0]["chat_link"] == CHAT
    settle(client, chat, "resolve", decide, resolution="Three retries", settled_by=CHI, chat_link=CHAT)
    approved = review(client, hub["other"], 1)
    assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
    # reopened after the approval, it holds the next one (question 2 of the checkpoint)
    text, error = settle(client, chat, "reopen", decide)
    assert not error, text
    retries = item(client, hub["chi"], "BR ORD2-1")
    saved(client, hub["chi"], 1, {"key": retries["key"], "body": "A failed payment can be retried 3 times."})
    assert client.delete(TYPE, headers=hub["owner"]).status_code == 200  # no review run for version 2
    submitted(client, hub["chi"], 2)
    held = review(client, hub["other"], 2)
    assert held.status_code == 409 and f"#{decide} (contradiction" in held.json()["message"], held.text
    reason = {"reason": "Settled in the release talk"}
    assert client.post(f"{FINDINGS}/{decide}/waive", json=reason, headers=hub["owner"]).status_code == 200
    assert review(client, hub["other"], 2).json()["state"] == "approved"


def test_srs_approval_gate_waits_for_the_review_run_of_the_round_unless_it_failed_or_was_cancelled(client, hub, hub_db):
    studio, run_id, _ = reviewing(client, hub)
    held = review(client, hub["other"], 1)
    assert held.status_code == 409, held.text
    assert f"while @ba's review of its round 1 (run #{run_id}) is running" in held.json()["message"], held.text
    moved(client, studio, run_id, "failed", error="the model was down")
    assert review(client, hub["other"], 1).json()["state"] == "approved", "a failed review holds nothing"
    # version 2: its first round's run is queued; a request for changes goes through, and the next round has its own
    cart = item(client, hub["chi"], "BR ORD1-1")
    saved(client, hub["chi"], 1, {"key": cart["key"], "body": "- A cart holds at most 40 lines."})
    submitted(client, hub["chi"], 2)
    v, r = tables.srs.versions, tables.runs
    ((second, state),) = sql(
        hub_db, select(v.c.review_run_id, r.c.state).join(r, r.c.id == v.c.review_run_id).where(v.c.number == 2)
    )
    assert state == "queued"
    held = review(client, hub["other"], 2)
    assert held.status_code == 409 and f"(run #{second}) is queued" in held.json()["message"], held.text
    returned = review(client, hub["other"], 2, "request_changes", body="Say why 40")
    assert returned.status_code == 200 and returned.json()["state"] == "draft", returned.text
    assert submitted(client, hub["chi"], 2)["round"] == 2
    ((again,),) = sql(hub_db, select(v.c.review_run_id).where(v.c.number == 2))
    assert again not in (None, second)
    held = review(client, hub["other"], 2)
    assert held.status_code == 409 and f"round 2 (run #{again}) is queued" in held.json()["message"], held.text
    for _ in (second, again):
        moved(client, studio, running(client, studio, "task")["id"], "verifying", "done", summary="Nothing found.")
    assert review(client, hub["other"], 2).json()["state"] == "approved", "round 2's review ended"
    # version 3: its review run is cancelled
    stock = item(client, hub["chi"], "BR ORD1-2")
    saved(client, hub["chi"], 2, {"key": stock["key"], "body": "- Placing the order holds its stock for 20 minutes."})
    submitted(client, hub["chi"], 3)
    ((third,),) = sql(hub_db, select(v.c.review_run_id).where(v.c.number == 3))
    assert review(client, hub["other"], 3).status_code == 409
    assert control(client, hub["owner"], third, "cancel").status_code == 200
    assert sql(hub_db, select(r.c.state).where(r.c.id == third)) == [("cancelled",)]
    assert review(client, hub["other"], 3).json()["state"] == "approved", "a cancelled review holds nothing"
