"""Memories as the web reads them: a web session reads by the grant's max level alone (``visible_by_grant``), a
machine token through a sink as before, and neither ever above the grant. A reader whose grant reaches internal
never receives a customer memory, by listing, searching its exact name, asking for its id or its history; another
member's user and feedback memories answer 404 exactly as a memory that does not exist; a revision whose label the
reader could not read is left out of the history.

The read rule without a sink is the hub's one grant-only rule (``ProjectRules.visible_by_grant`` and its
``grant_label``, which the knowledge graph pages read with too), so its tests live here. It runs without Postgres;
everything that needs the hub skips without EVO_HUB_TEST_DSN."""

import itertools
import re

import pytest

from evo_agents.hub.access import ProjectRules
from evo_agents.hub.memory import AGENT_SINK
from tests.hub import live, pg

LEVELS = ["public", "internal", "customer", "secret"]
LOCATIONS = ["any", "domestic-only"]
SINKS = [
    {"id": AGENT_SINK, "kind": "agent-session", "clearance": {"level": "internal"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "secret", "location": "domestic-only"}},
    {"id": "export", "kind": "writeback-target", "clearance": {"level": "public", "location": "any"}},
]

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


# The read rule without a sink


def rules() -> ProjectRules:
    return ProjectRules("demo", LEVELS, LOCATIONS, SINKS, {"level": "internal"})


LABELS = [
    {"level": level, "location": location, "integrity": integrity, "projects": projects}
    for level, location, integrity, projects in itertools.product(
        LEVELS, LOCATIONS, ("T", "U"), (["demo"], ["demo", "other"], ["other"], [])
    )
]


def test_visible_by_grant_is_the_grant_label_and_nothing_else():
    project = rules()
    for label, max_level in itertools.product(LABELS, [*LEVELS, None, "top-secret"]):
        expected = (
            max_level in LEVELS
            and LEVELS.index(label["level"]) <= LEVELS.index(max_level)
            and set(label["projects"]) <= {"demo"}
        )
        assert project.visible_by_grant(label, max_level) is expected, (label, max_level)
    assert len(LABELS) * 6 == 384


def test_visible_by_grant_lets_through_whatever_a_sink_does_and_more_only_up_to_the_grant():
    project = rules()
    for label, max_level, sink in itertools.product(LABELS, LEVELS, [s["id"] for s in SINKS]):
        if project.visible(label, max_level, sink):
            assert project.visible_by_grant(label, max_level), (label, max_level, sink)
    # Where the sink narrows, the grant alone does not: a customer label, a customer grant, Claude Code's sink.
    customer = {"level": "customer", "location": "any", "projects": ["demo"]}
    assert not project.visible(customer, "customer", AGENT_SINK)
    assert project.visible_by_grant(customer, "customer")
    assert not project.visible_by_grant(customer, "internal")


def test_visible_by_grant_fails_closed_on_labels_it_cannot_read():
    project = rules()
    for broken in (None, "internal", [], {"level": "public", "projects": "demo"}, {"projects": [1]}):
        assert not project.visible_by_grant(broken, "secret"), broken
    # A level or location the ladder lacks reads as the highest: only a grant of the top level sees it.
    assert not project.visible_by_grant({"level": "galaxy"}, "customer")
    assert project.visible_by_grant({"level": "galaxy"}, "secret")
    assert project.visible_by_grant({"level": "public", "location": "mars"}, "public")  # every location is granted
    # Without projects a label belongs to this project; integrity never narrows a grant.
    assert project.visible_by_grant({"level": "public"}, "public")
    assert project.visible_by_grant({"level": "public", "integrity": "U"}, "public")
    assert project.grant_label(None) is None and project.grant_label("nope") is None


@pytest.mark.parametrize("max_level", LEVELS)
def test_the_grant_label_reaches_its_level_everywhere_in_its_project_and_the_sink_rule_meets_it(max_level):
    """The label the knowledge graph pages read with: the grant's level, every location, this project alone; for any
    sink, the sink rule's ceiling is that label met with the sink's clearance."""
    project = rules()
    grant = project.grant_label(max_level)
    assert (grant.level, grant.location, grant.projects) == (LEVELS.index(max_level), 1, frozenset({"demo"}))
    for sink in (s["id"] for s in SINKS):
        clearance = project.policy.clearance(sink)
        sink_label = type(grant)(clearance.level, clearance.location, "U", frozenset({"demo"}))
        assert project.ceiling(max_level, sink) == grant.meet(sink_label), sink


@pytest.mark.parametrize("max_level", [None, "", "top-secret", 2])
def test_no_grant_or_a_level_the_ladder_lacks_lets_nothing_through(max_level):
    project = rules()
    assert project.grant_label(max_level) is None
    assert not project.visible_by_grant({"level": "public"}, max_level)


def test_a_web_session_reads_through_no_sink_and_a_machine_token_through_claude_codes():
    pytest.importorskip("fastapi")
    from evo_agents.hub.server.memories import _through
    from evo_agents.hub.server.security import MACHINE, WEB, Principal

    def principal(kind: str) -> Principal:
        return Principal(user_id=1, login="someone", admin=False, token_id=1, kind=kind, token_hash="x")

    assert _through(principal(WEB), None) is None
    assert _through(principal(MACHINE), None) == AGENT_SINK
    assert _through(principal(WEB), "export") == "export"  # a sink the caller names always narrows
    assert _through(principal(MACHINE), "hub") == "hub"


# The server

PROJECT = {
    "levels": LEVELS,
    "locations": LOCATIONS,
    "default_label": {"level": "internal"},
    "sinks": SINKS,
    "repos": [{"name": "app", "path": "app"}, {"name": "web", "path": "web"}],
    "harness": {"name": "demo", "workspace": "~/ws", "path": "demo-harness"},
}
GRANTS = {
    "alice": ("writer", "secret"),
    "dave": ("reader", "customer"),
    "carol": ("reader", "internal"),
    "erin": ("reader", "public"),
}


@pytest.fixture
def client(hub_db, tmp_path, github):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app

    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github))) as client:
        yield client


@pytest.fixture
def who(client, hub_db) -> dict:
    """Headers by login and credential: ``who[login]`` a machine token, ``who[login, "web"]`` a web session."""
    from evo_agents.hub.server.security import SESSION_COOKIE, WEB

    logins = (live.ADMIN, *GRANTS, "stranger")
    found = {login: live.bearer(live.insert_token(hub_db, login)) for login in logins}
    for login in logins:
        found[login, "web"] = {"Cookie": f"{SESSION_COOKIE}={live.insert_token(hub_db, login, kind=WEB)}"}
    assert client.put("/v1/projects/demo", json=PROJECT, headers=found[live.ADMIN]).status_code == 200
    for login, (role, level) in GRANTS.items():
        grant = {"role": role, "max_level": level}
        response = client.put(f"/v1/admin/projects/demo/grants/{login}", json=grant, headers=found[live.ADMIN])
        assert response.status_code == 200, response.text
    return found


def put(client, headers, **fields) -> dict:
    body = {
        "scope": "project",
        "project": "demo",
        "location": "harness",
        "name": "note.md",
        "type": "project",
        "body": "---\nname: Note\n---\nthe note\n",
        **fields,
    }
    # Through the hub sink, which clears every level here: the writer reads back whatever it writes.
    response = client.put("/v1/memories", params={"sink": "hub"}, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def listed(client, headers, **params) -> list[str]:
    response = client.get("/v1/memories", params={"limit": 500, **params}, headers=headers)
    assert response.status_code == 200, response.text
    return sorted(m["name"] for m in response.json()["items"])


def searched(client, headers, q: str, **params) -> list[str]:
    response = client.get("/v1/memories/search", params={"q": q, "limit": 50, **params}, headers=headers)
    assert response.status_code == 200, response.text
    return sorted(m["name"] for m in response.json()["items"])


def same_as_missing(client, headers, path: str, hidden_id: int) -> None:
    """``path`` with ``hidden_id`` answers exactly as the same path with an id that does not exist."""
    hidden = client.get(path.format(hidden_id), headers=headers)
    missing = client.get(path.format(999_999), headers=headers)
    assert hidden.status_code == missing.status_code == 404, hidden.text
    assert hidden.json()["error"] == missing.json()["error"] == "not_found"

    def normalized(message: str, value: int) -> str:
        return re.sub(rf"\b{value}\b", "N", message, count=1)  # the first number the message names is the id

    assert normalized(hidden.json()["message"], hidden_id) == normalized(missing.json()["message"], 999_999)


LABELLED = {level: f"{level}-plan.md" for level in LEVELS}


@needs_pg
def test_a_reader_never_receives_a_memory_above_the_grant_even_by_its_exact_name(client, who):
    ids = {}
    for level, name in LABELLED.items():
        label = {"level": level, "location": "domestic-only"}
        ids[level] = put(client, who["alice"], name=name, body=f"# {level}\nkangaroo {level}\n", label=label)["id"]

    # A web session reads by the grant alone.
    assert listed(client, who["carol", "web"], project="demo") == ["internal-plan.md", "public-plan.md"]
    assert listed(client, who["dave", "web"], project="demo") == [
        "customer-plan.md",
        "internal-plan.md",
        "public-plan.md",
    ]
    assert listed(client, who["erin", "web"], project="demo") == ["public-plan.md"]
    assert listed(client, who["alice", "web"]) == sorted(LABELLED.values())
    # Searching the exact name, or the words only it holds, finds nothing above the grant.
    for q in ("customer-plan.md", "customer", '"kangaroo customer"', "secret"):
        assert searched(client, who["carol", "web"], q, project="demo") == [], q
    assert searched(client, who["carol", "web"], "kangaroo") == ["internal-plan.md", "public-plan.md"]
    # Asked for by id, or for its history, it answers as a memory that does not exist.
    for level in ("customer", "secret"):
        for path in ("/v1/memories/{}", "/v1/memories/{}/revisions", "/v1/memories/{}/revisions/1"):
            same_as_missing(client, who["carol", "web"], path, ids[level])
    assert client.get(f"/v1/memories/{ids['internal']}", headers=who["carol", "web"]).status_code == 200

    # A machine token still reads through Claude Code's sink, which clears internal: the grant reaching customer
    # does not let a customer memory into a session.
    assert listed(client, who["dave"], project="demo") == ["internal-plan.md", "public-plan.md"]
    assert client.get(f"/v1/memories/{ids['customer']}", headers=who["dave"]).status_code == 404
    assert client.get(f"/v1/memories/{ids['customer']}", headers=who["dave", "web"]).status_code == 200
    # A sink the caller names narrows a web session too.
    assert listed(client, who["dave", "web"], project="demo", sink="export") == []
    assert listed(client, who["dave", "web"], project="demo", sink=AGENT_SINK) == ["internal-plan.md", "public-plan.md"]
    # A hub admin without a grant reads nothing; a member of no project does not see the project.
    assert listed(client, who[live.ADMIN, "web"], project="demo") == []
    assert client.get("/v1/memories", params={"project": "demo"}, headers=who["stranger", "web"]).status_code == 404
    same_as_missing(client, who["stranger", "web"], "/v1/memories/{}", ids["public"])


@needs_pg
def test_user_and_feedback_memories_stay_their_owners_on_the_web(client, who):
    created = {}
    for kind in ("user", "feedback", "project"):
        created[kind] = put(client, who["alice"], name=f"alice-{kind}.md", type=kind, body=f"wombat {kind}\n")
    personal = client.put(
        "/v1/memories",
        json={"scope": "personal", "location": "notes", "name": "alice-diary.md", "type": "user", "body": "wombat"},
        headers=who["alice"],
    )
    assert personal.status_code == 200, personal.text
    created["personal"] = personal.json()

    assert listed(client, who["alice", "web"]) == [
        "alice-diary.md",
        "alice-feedback.md",
        "alice-project.md",
        "alice-user.md",
    ]
    assert listed(client, who["alice", "web"], scope="personal") == ["alice-diary.md"]
    for reader in ("dave", "carol"):
        assert listed(client, who[reader, "web"]) == ["alice-project.md"]
        assert listed(client, who[reader, "web"], scope="personal") == []
        assert searched(client, who[reader, "web"], "wombat") == ["alice-project.md"]
        for name in ("alice-user.md", "alice-feedback.md", "alice-diary.md"):
            assert searched(client, who[reader, "web"], name) == [], name
        for kind in ("user", "feedback", "personal"):
            for path in ("/v1/memories/{}", "/v1/memories/{}/revisions", "/v1/memories/{}/revisions/1"):
                same_as_missing(client, who[reader, "web"], path, created[kind]["id"])
    mine = client.get(f"/v1/memories/{created['user']['id']}/revisions", headers=who["alice", "web"])
    assert mine.status_code == 200 and [r["revision"] for r in mine.json()["items"]] == [1]


@needs_pg
def test_the_history_leaves_out_revisions_above_the_readers_grant(client, who):
    customer = {"level": "customer", "location": "domestic-only"}
    internal = {"level": "internal", "location": "domestic-only"}
    first = put(client, who["alice"], name="rollout.md", body="draft naming the customer ACME\n", label=customer)
    memory_id = first["id"]
    second = put(
        client, who["alice"], name="rollout.md", body="cleaned: no customer name\n", label=internal, if_revision=1
    )
    third = put(client, who["alice"], name="rollout.md", body="cleaned, phần hai\n", label=internal, if_revision=2)
    assert (second["revision"], third["revision"]) == (2, 3)

    def history(login: str, **params) -> dict:
        response = client.get(f"/v1/memories/{memory_id}/revisions", params=params, headers=who[login, "web"])
        assert response.status_code == 200, response.text
        return response.json()

    full = history("dave")
    assert [r["revision"] for r in full["items"]] == [3, 2, 1] and full["next_before"] is None
    newest = full["items"][0]
    assert newest["actor"] == "alice" and newest["label"]["level"] == "internal" and not newest["deleted"]
    assert newest["size"] == len("cleaned, phần hai\n".encode()) and "body" not in newest
    old = client.get(f"/v1/memories/{memory_id}/revisions/1", headers=who["dave", "web"])
    assert old.status_code == 200 and old.json()["body"] == "draft naming the customer ACME\n"

    # carol's grant reaches internal: the customer revision is not in the history, and asking for it is a 404.
    assert [r["revision"] for r in history("carol")["items"]] == [3, 2]
    same_as_missing(client, who["carol", "web"], f"/v1/memories/{memory_id}/revisions/{{}}", 1)
    hidden = client.get(f"/v1/memories/{memory_id}/revisions/1", headers=who["carol", "web"])
    assert "ACME" not in hidden.text
    older = client.get(f"/v1/memories/{memory_id}/revisions/2", headers=who["carol", "web"])
    assert older.status_code == 200 and older.json()["body"] == "cleaned: no customer name\n"
    assert client.get(f"/v1/memories/{memory_id}/revisions/4", headers=who["dave", "web"]).status_code == 404
    # erin's grant reaches public: the memory itself is hidden, and so its whole history.
    same_as_missing(client, who["erin", "web"], "/v1/memories/{}/revisions", memory_id)

    # Pages, the latest first.
    page = history("carol", limit=1)
    assert [r["revision"] for r in page["items"]] == [3] and page["next_before"] == 3
    page = history("carol", limit=1, before=3)
    assert [r["revision"] for r in page["items"]] == [2] and page["next_before"] == 2
    page = history("carol", limit=1, before=2)
    assert page["items"] == [] and page["next_before"] is None

    # A deletion is a revision too: a tombstone without a body.
    deleted = client.delete(f"/v1/memories/{memory_id}?if_revision=3", headers=who["alice"])
    assert deleted.status_code == 200, deleted.text
    tombstone = history("carol")["items"][0]
    assert (tombstone["revision"], tombstone["deleted"], tombstone["size"]) == (4, True, 0)
    assert client.get(f"/v1/memories/{memory_id}", headers=who["carol", "web"]).json()["deleted"] is True
    assert listed(client, who["carol", "web"], project="demo") == []


@needs_pg
def test_search_narrows_to_a_location_and_answers_every_reader_alike(client, who):
    put(client, who["alice"], name="harness-note.md", location="harness", body="platypus in the harness\n")
    put(client, who["alice"], name="app-note.md", location="app", body="platypus in the app\n")
    assert searched(client, who["carol", "web"], "platypus", project="demo") == ["app-note.md", "harness-note.md"]
    assert searched(client, who["carol", "web"], "platypus", project="demo", location="app") == ["app-note.md"]
    assert searched(client, who["carol"], "platypus", location="harness") == ["harness-note.md"]
    assert listed(client, who["carol", "web"], project="demo", location="app") == ["app-note.md"]
    refused = client.get("/v1/memories/search", params={"q": "x", "location": ""}, headers=who["carol", "web"])
    assert refused.status_code == 422


@needs_pg
def test_a_run_scope_reads_and_writes_the_memories_of_its_project_alone(client, who, hub_db):
    """The agent of a run reaches these routes, through the hub's /mcp, as its owner with the run's scope
    (``RunScope``): the memories of the run's project, under the grant as the scope caps it, and never a personal
    memory or one of another project, by listing, searching, asking for an id, deleting or writing."""
    from dataclasses import replace
    from functools import partial

    from fastapi import HTTPException
    from starlette.requests import Request

    from evo_agents.hub.server import memories
    from evo_agents.hub.server.projects import project_access
    from evo_agents.hub.server.security import WORKER, Principal, RunScope

    other = {**PROJECT, "harness": {"name": "other", "workspace": "~/ws", "path": "other-harness"}}
    assert client.put("/v1/projects/other", json=other, headers=who[live.ADMIN]).status_code == 200
    grant = {"role": "writer", "max_level": "secret"}
    response = client.put("/v1/admin/projects/other/grants/alice", json=grant, headers=who[live.ADMIN])
    assert response.status_code == 200, response.text
    ids = {}
    for level in ("internal", "customer"):
        label = {"level": level, "location": "domestic-only"}
        ids[level] = put(client, who["alice"], name=f"{level}-note.md", body=f"quokka {level}\n", label=label)["id"]
    ids["other"] = put(client, who["alice"], project="other", name="other-note.md", body="quokka other\n")["id"]
    diary = {"scope": "personal", "location": "notes", "name": "diary.md", "type": "user", "body": "quokka diary\n"}
    personal = client.put("/v1/memories", json=diary, headers=who["alice"])
    assert personal.status_code == 200, personal.text
    ids["personal"] = personal.json()["id"]
    everything = ["customer-note.md", "diary.md", "internal-note.md", "other-note.md"]
    assert searched(client, who["alice"], "quokka", sink="hub") == everything  # alice's own machine token

    from sqlalchemy import select

    from evo_agents.hub import tables

    users, tokens = tables.users, tables.tokens
    (user_id, token_id), *_ = live.sql(
        hub_db,
        select(users.c.id, tokens.c.id)
        .join_from(users, tokens, tokens.c.user_id == users.c.id)
        .where(users.c.login == "alice"),
    )
    agent = Principal(user_id, "alice", False, token_id, WORKER, "x", RunScope(7, "demo", "writer", "internal"))
    request = Request(
        {"type": "http", "app": client.app, "method": "GET", "path": "/", "headers": [], "query_string": b""}
    )

    def call(route, user=agent, **arguments):
        return client.portal.call(partial(route, request, user=user, **arguments))

    def refused(route, **arguments) -> HTTPException:
        with pytest.raises(HTTPException) as caught:
            call(route, **arguments)
        return caught.value

    def names(found) -> list[str]:
        return sorted(memory.name for memory in found.items)

    # The grant reaches secret and the hub sink clears it; the scope stops at internal.
    assert names(call(memories.list_memories, sink="hub")) == ["internal-note.md"]
    assert names(call(memories.search_memories, q="quokka", sink="hub")) == ["internal-note.md"]
    assert names(call(memories.search_memories, q="quokka", scope="personal", sink="hub")) == []
    assert names(call(memories.list_memories, scope="personal", sink="hub")) == []
    assert call(memories.show, memory_id=ids["internal"], sink="hub").name == "internal-note.md"
    for hidden in ("customer", "other", "personal"):
        assert refused(memories.show, memory_id=ids[hidden], sink="hub").status_code == 404, hidden
        assert refused(memories.revisions, memory_id=ids[hidden], sink="hub").status_code == 404, hidden
    assert refused(memories.delete, memory_id=ids["personal"], if_revision=1, sink="hub").status_code == 404
    elsewhere = refused(memories.list_memories, project="other", sink="hub")
    assert elsewhere.status_code == 404 and "no project other" in elsewhere.detail
    assert refused(memories.search_memories, q="quokka", project="other", sink="hub").status_code == 404

    note = {"scope": "project", "project": "demo", "location": "harness", "name": "agent.md", "type": "project"}
    note["body"] = "---\nname: Agent\n---\nquokka agent\n"
    written = call(memories.put, body=memories.MemoryIn(**note), sink="hub")
    assert written.created and (written.project, written.updated_by) == ("demo", "alice")
    assert refused(memories.put, body=memories.MemoryIn(**{**note, "project": "other"}), sink="hub").status_code == 404
    mine = refused(memories.put, body=memories.MemoryIn(**{**diary, "name": "agent-diary.md"}), sink="hub")
    assert mine.status_code == 403 and "a personal memory is its owner's" in mine.detail
    stored = live.sql(hub_db, select(tables.memories.c.name))
    assert sorted(row[0] for row in stored) == sorted([*everything, "agent.md"])

    # The scope caps the grant, role and level alike: a reader's scope writes nothing.
    reader = replace(agent, scope=RunScope(7, "demo", "reader", "public"))
    assert (
        refused(memories.put, user=reader, body=memories.MemoryIn(**{**note, "name": "r.md"}), sink="hub").status_code
        == 403
    )
    assert names(call(memories.list_memories, user=reader, sink="hub")) == []

    async def grant_of(user) -> tuple:
        async with client.app.state.engine.begin() as conn:
            access = await project_access(conn, user, "demo")
        return access.role, access.max_level

    assert client.portal.call(grant_of, agent) == ("writer", "internal")
    assert client.portal.call(grant_of, reader) == ("reader", "public")
    assert client.portal.call(grant_of, replace(agent, scope=None)) == ("writer", "secret")
