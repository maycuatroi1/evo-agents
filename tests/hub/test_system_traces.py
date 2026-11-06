"""The traces of an agent system (``evo_agents.hub.server.system_traces`` and ``system_trace_views``), as criterion a5
of plan multi-agent-systems names it: a person's message to the entry agent opens a trace in any chat, a direct chat
included; the runs it wakes join it through mentions and asks; each turn and each ask or mention is a span named as
OpenTelemetry GenAI names them; an edge the spec lacks is off_design, and an agent the spec lacks an external node; a
trace follows its chat's audience and withholds the content of another project's spans; metrics per node and edge;
a live stream on the process's LISTEN. Invariant a2: a mention off the edges still runs.
"""

from __future__ import annotations

import json
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select

from evo_agents.hub import system_spec, tables
from evo_agents.hub.server import system_trace_views, system_traces
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401
    OTHER,
    PROJECT,
    app_key,
    chat_worker,
    claim,
    client,
    created,
    ended,
    hub,
    in_chats,
    moved,
    reply,
    sent,
    started,
)

SLUG = "review"
SPEC = {
    "schema": system_spec.SCHEMA,
    "name": "Review desk",
    "purpose": "Review and fix the changes of the project.",
    "nodes": [
        {"id": "in", "kind": "entry"},
        {"id": "review", "kind": "agent", "profile": "reviewer"},
        {"id": "fix", "kind": "agent", "profile": "fixer"},
    ],
    "edges": [
        {"from": "in", "to": "review", "kind": "interacts", "description": "every change to review"},
        {"from": "review", "to": "fix", "kind": "interacts", "description": "the fixes a review asks for"},
    ],
}


def put_system(client, headers, spec: dict = SPEC, slug: str = SLUG) -> dict:
    response = client.put(f"/v1/projects/{PROJECT}/systems/{slug}", json={"spec": spec}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def traces_path(slug: str = SLUG) -> str:
    return f"/v1/projects/{PROJECT}/systems/{slug}/traces"


def spans_of(db, trace_id: int | None = None) -> list[dict]:
    s = tables.system_spans
    query = select(s).order_by(s.c.id)
    if trace_id is not None:
        query = query.where(s.c.trace_id == trace_id)
    return [dict(zip(s.c.keys(), row, strict=True)) for row in sql(db, query)]


def invoked(db, run_id: int) -> dict:
    (found,) = [span for span in spans_of(db) if span["run_id"] == run_id and span["name"] == "invoke_agent"]
    return found


def mentioned(client, worker: dict, run_id: int, handle: str, body: str) -> dict:
    handed = in_chats(client, worker, run_id, "chat/mentions", {"handle": handle, "body": body})
    assert handed.status_code == 201, handed.text
    return handed.json()


def test_a_persons_message_to_the_entry_agent_opens_a_trace_its_runs_join_and_off_edges_still_run(client, hub, hub_db):
    put_system(client, hub["owner"])
    studio = chat_worker(client, hub["owner"], "studio", slots=3)
    fixer = chat_worker(client, hub["owner"], "fixer-node", pool="fixer")
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer", "fixer"])
    mention, spec = started(client, hub, studio, chat)
    parent = spec["id"]
    assert "# Your agent system: Review desk (review)" in spec["prompt"]
    assert "Agents you hand work to (with `evo-agents worker agent ask`):\n- @fixer: the fixes" in spec["prompt"]
    t = tables.system_traces
    ((trace_id, slug, chat_id, message_id),) = sql(hub_db, select(t.c.id, t.c.system_slug, t.c.chat_id, t.c.message_id))
    assert (slug, chat_id, message_id) == (SLUG, chat["id"], mention["id"])
    first = invoked(hub_db, parent)
    assert (first["node"], first["from_node"], first["parent_id"], first["off_design"]) == ("review", "in", None, False)
    assert first["attributes"]["gen_ai.operation.name"] == "invoke_agent"
    assert first["attributes"]["gen_ai.agent.name"] == "reviewer"
    assert first["attributes"]["gen_ai.conversation.id"] == str(chat["id"])

    # on the edge review -> fix
    handed = mentioned(client, studio, parent, "fixer", "fix the retry loop")
    child = handed["run_id"]
    assert child is not None
    second = invoked(hub_db, child)
    assert (second["node"], second["from_node"], second["parent_id"], second["off_design"]) == (
        "fix",
        "review",
        first["id"],
        False,
    )
    tool = [s for s in spans_of(hub_db) if s["tool"] == "chat_mention"]
    assert [(s["to_node"], s["off_design"], s["attributes"]["evo.child_run_id"]) for s in tool] == [
        ("fix", False, child)
    ]
    moved(client, studio, parent, "verifying", "done")
    assert invoked(hub_db, parent)["status"] == "ok" and invoked(hub_db, parent)["ended_at"] is not None

    # off the edges (fix -> review): it runs, recorded off design with no reason (a2)
    assert claim(client, fixer)["id"] == child
    moved(client, fixer, child, "running", session_id="sess-fix")
    back = mentioned(client, fixer, child, "reviewer", "review the fix again")
    assert back["refusal"] is None and back["run_id"] is not None
    third = invoked(hub_db, back["run_id"])
    assert (third["node"], third["from_node"], third["off_design"], third["reason"]) == ("review", "fix", True, None)
    assert third["trace_id"] == trace_id

    # the API: the trace, its spans and metrics
    listed = client.get(traces_path(), headers=hub["owner"])
    assert listed.status_code == 200, listed.text
    (summary,) = listed.json()
    assert (summary["id"], summary["runs"], summary["off_design"], summary["opened_by"]) == (trace_id, 3, 2, "owner")
    shown = client.get(f"{traces_path()}/{trace_id}", headers=hub["owner"]).json()
    assert [s["name"] for s in shown["spans"]].count("invoke_agent") == 3
    assert {s["tool"] for s in shown["spans"] if s["name"] == "execute_tool"} == {"chat_mention"}
    edges = {(e["from_node"], e["to_node"]): e for e in shown["metrics"]["edges"]}
    assert edges[("in", "review")]["count"] == 1 and edges[("review", "fix")]["off_design"] == 0
    assert edges[("fix", "review")]["off_design"] == 1
    nodes = {n["node"]: n for n in shown["metrics"]["nodes"]}
    assert nodes["review"]["count"] == 2 and nodes["fix"]["count"] == 1
    metrics = client.get(f"/v1/projects/{PROJECT}/systems/{SLUG}/metrics", headers=hub["owner"]).json()
    assert metrics == shown["metrics"]


def test_a_direct_chat_opens_a_trace_that_follows_its_audience(client, hub, hub_db):
    put_system(client, hub["owner"])
    studio = chat_worker(client, hub["owner"], "studio", slots=2)
    opened = client.put("/v1/chats/direct/reviewer", headers=hub["owner"])
    assert opened.status_code in (200, 201), opened.text
    direct = opened.json()
    sent(client, hub["owner"], direct["id"], "please review the wait helper")
    run = claim(client, studio)
    assert run is not None and run["kind"] == "chat"
    t = tables.system_traces
    assert sql(hub_db, select(t.c.chat_id)) == [(direct["id"],)]
    # owner reads it; someone-else, a writer of the project outside the chat, does not; a stranger is refused
    assert len(client.get(traces_path(), headers=hub["owner"]).json()) == 1
    assert client.get(traces_path(), headers=hub["other"]).json() == []
    (trace_id,) = sql(hub_db, select(t.c.id))[0]
    assert client.get(f"{traces_path()}/{trace_id}", headers=hub["other"]).status_code == 404
    assert client.get(traces_path(), headers=hub["stranger"]).status_code in (403, 404)


def test_an_external_agent_is_an_off_design_node_and_its_content_is_withheld_without_a_grant(client, hub, hub_db):
    from tests.hub.test_agent_delegation import KB, KB_AGENT, kb_node, kb_project

    kb_project(client, hub, hub_db, role=None)  # someone-else holds no grant on lumen-kb
    put_system(client, hub["owner"])
    studio = chat_worker(client, hub["owner"], "studio", slots=2)
    kb_node(client, hub["owner"], hub_db)
    chat = created(client, hub["owner"], people=[OTHER])
    _, spec = started(client, hub, studio, chat)
    body = {"handle": KB_AGENT, "body": "Where is the export key read?", "reason": "the docs of lumen-kb hold it"}
    asked = in_chats(client, studio, spec["id"], "agent/asks", body)
    assert asked.status_code == 201, asked.text
    child = asked.json()["run_id"]
    assert child is not None
    span = invoked(hub_db, child)
    assert (span["node"], span["from_node"], span["off_design"], span["reason"]) == (
        f"@{KB_AGENT}",
        "review",
        True,
        "the docs of lumen-kb hold it",
    )
    (trace_id,) = sql(hub_db, select(tables.system_traces.c.id))[0]
    owner = client.get(f"{traces_path()}/{trace_id}", headers=hub["owner"]).json()
    other = client.get(f"{traces_path()}/{trace_id}", headers=hub["other"]).json()
    (seen,) = [s for s in owner["spans"] if s["run_id"] == child and s["name"] == "invoke_agent"]
    (hidden,) = [s for s in other["spans"] if s["run_id"] == child and s["name"] == "invoke_agent"]
    assert (seen["project"], seen["content_withheld"], seen["reason"]) == (KB, False, "the docs of lumen-kb hold it")
    assert seen["message_id"] is not None
    assert (hidden["content_withheld"], hidden["reason"], hidden["message_id"]) == (True, None, None)
    assert hidden["node"] == f"@{KB_AGENT}" and hidden["off_design"] is True
    assert all(key.startswith("gen_ai.") for key in hidden["attributes"])


def test_the_kg_tools_of_a_system_agent_default_to_the_graphs_it_uses(client, hub, hub_db):
    graph = client.post(f"/v1/kg/{PROJECT}/graphs", json={"name": "docs"}, headers=hub["owner"])
    assert graph.status_code == 201, graph.text
    spec = {
        **SPEC,
        "nodes": [*SPEC["nodes"], {"id": "docs", "kind": "kg", "graph": "docs"}],
        "edges": [*SPEC["edges"], {"from": "review", "to": "docs", "kind": "uses_kg", "description": "the docs"}],
    }
    put_system(client, hub["owner"], spec)
    studio = chat_worker(client, hub["owner"], "studio", slots=2)
    chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
    _, run = started(client, hub, studio, chat)
    assert "Knowledge graphs you use" in run["prompt"] and "- docs: the docs" in run["prompt"]
    engine = client.app.state.engine
    (project_id,) = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0]

    async def default():
        async with engine.begin() as conn:
            found = await system_traces.default_graph(conn, run["id"], project_id)
            await system_traces.record_kg(conn, run["id"], "kg_search", found[0] if found else None)
            return found

    assert client.portal.call(partial(default)) == ("system:review", frozenset())
    (span,) = [s for s in spans_of(hub_db) if s["tool"] == "kg_search"]
    assert span["attributes"]["gen_ai.data_source.id"] == "system:review"
    assert span["attributes"]["gen_ai.operation.name"] == "execute_tool"


def test_an_agent_in_no_system_runs_with_no_trace_and_no_system_section(client, hub, hub_db):
    studio = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    _, spec = started(client, hub, studio, chat)
    assert "# Your agent system" not in spec["prompt"] and "# Your brief" not in spec["prompt"]
    assert sql(hub_db, select(tables.system_traces.c.id)) == []
    assert spans_of(hub_db) == []


# The live stream


def test_the_trace_stream_wakes_on_a_span_written(hub_db, tmp_path, github, monkeypatch):
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub import live
    from tests.hub.test_chats import ADMIN_GRANT, OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import members, serving, wait_for
    from tests.hub.test_runs import OWNER

    monkeypatch.setattr(system_trace_views, "STREAM_POLL_SECONDS", 60.0)  # only the notification wakes it in time
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as http:
        headers = members(http, github)
        assert http.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"]).is_success
        grant = http.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
        assert grant.is_success
        for handle in ("reviewer", "fixer"):
            made = http.post(
                f"/v1/projects/{PROJECT}/agents",
                json={"handle": handle, "name": handle.title()},
                headers=headers["owner"],
            )
            assert made.status_code == 201, made.text
        assert http.put(f"{traces_path().rsplit('/', 1)[0]}", json={"spec": SPEC}, headers=headers["owner"]).is_success
        s, t = tables.agent_systems, tables.system_traces
        ((system_id, project_id, revision),) = sql(hub_db, select(s.c.id, s.c.project_id, s.c.revision))
        with http.stream("GET", f"{traces_path()}/stream", headers=headers["owner"]) as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            wait_for(lambda: app.state.trace_streams.open_streams() == 1, what="the stream to subscribe")
            values = {
                "project_id": project_id,
                "system_id": system_id,
                "system_slug": SLUG,
                "system_revision": revision,
            }
            (trace_id,) = sql(hub_db, insert(t).values(**values).returning(t.c.id))[0]
            sql(hub_db, select(func.pg_notify(system_traces.CHANNEL, str(system_id))))
            event: dict = {}
            for line in lines:
                if line.startswith(":"):
                    continue
                if line == "" and event:
                    break
                field, _, value = line.partition(":")
                if field:
                    event[field] = value.removeprefix(" ")
            assert event["event"] == "trace"
            assert json.loads(event["data"])["id"] == trace_id
