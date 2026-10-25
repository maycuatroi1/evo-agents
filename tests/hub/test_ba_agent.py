"""@ba, the agent type ba of the core plugin srs, on the hub (``evo_agents.hub.server.srs.agent``,
``auto_review``, ``evo_agents.hub.server.chat_route``), as criterion a7 of the plan srs-tasks-plugins names it.

An admin of a project that enabled srs enables the type under a member (``run_as``): the profile @ba with the pack
srs/ba. The plugin's tools are on the hub's /mcp only where the project enables it. Every chat run of @ba goes to a
worker of run_as, also when the writer who mentioned it has none, and acts for that writer: a proposal is the
document's next version, in review at once, written as the writer, who therefore never approves it; the agent of a
run approves nothing. Each time a version enters review, in every round, it is handed to @ba as a task run on
run_as's worker, whose comments are advice that blocks no approval (``tests.hub.test_srs_auto_review`` has the rest of
that review). A task of a use case is made for the writer who asked,
only while they hold writer. A type that does not route its chat runs to run_as, or @ba while its type is off, routes
as before: to the worker of the person who mentioned it. An archived document is out of @ba's list and search, still
read by its slug, and takes no proposal or comment (criterion a1 of plan srs-document-lifecycle). The agent on the
worker is a stand-in: the test calls the tools with the worker's token and the run, as its MCP client would. The pack's
files are ``tests.worker.test_ba_agent``'s.
"""

from __future__ import annotations

import dataclasses
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import plugin_calls, plugin_events, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.srs import BA_INSTRUCTIONS
from evo_agents.hub.server.srs import MANIFEST as SRS_MANIFEST
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chat_runs import chat_worker, created, run_of, sent
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_mcp import call
from tests.hub.test_plugin_points import rpc, tool_names
from tests.hub.test_plugin_runtimes import set_plugins
from tests.hub.test_runs import PROJECT, claim, moved

app_key = credentials_api.app_key  # a fixture
OWNER, OTHER, CHI, READER = credentials_api.OWNER, credentials_api.OTHER, "chi", "reader"
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
WRITER = credentials_api.WRITER
READ_ONLY = {"role": "reader", "max_level": "internal"}
CORE = {
    "srs": EntryPoint("srs", "evo_agents.hub.server.srs:MANIFEST", ENTRY_POINT_GROUP),
    "tasks": EntryPoint("tasks", "evo_agents.hub.server.tasks:MANIFEST", ENTRY_POINT_GROUP),
}
SAMPLE = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
SRS = f"/v1/x/srs/{PROJECT}"
DOC = f"{SRS}/documents/demo-shop"
TYPE = f"/v1/projects/{PROJECT}/agent-types/srs/ba"
SRS_TOOLS = ["srs__comment", "srs__diff", "srs__finding", "srs__propose", "srs__read", "srs__search"]


@pytest.fixture
def srs_core(monkeypatch):
    """The entry points of the core plugins with srs's and tasks's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3, srs_core):  # noqa: F811
    config = dataclasses.replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def members(client, github) -> dict:
    """The project of ``test_credentials_api``, owner its admin, someone-else and chi writers, reader a reader, the
    hub admin with no grant; the agent @reviewer, of no agent type. The headers of each."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
        "chi": bearer(live.sign_in(client, github, CHI, 505)["token"]),
        "reader": bearer(live.sign_in(client, github, READER, 504)["token"]),
    }
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, ADMIN_GRANT), (OTHER, WRITER), (CHI, WRITER), (READER, READ_ONLY)):
        granted(client, headers, login, grant)
    made = client.post(
        f"/v1/projects/{PROJECT}/agents",
        json={"handle": "reviewer", "name": "Reviewer", "instructions": "Read the diff."},
        headers=headers["owner"],
    )
    assert made.status_code == 201, made.text
    return headers


@pytest.fixture
def hub(client, members) -> dict:
    """``members`` with plugin srs enabled in the project."""
    enable(client, members["owner"], "srs")
    return members


def granted(client, headers, login: str, grant: dict) -> None:
    response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
    assert response.status_code == 200, response.text


def enable(client, headers, plugin: str, permissions=()) -> None:
    body = {"permissions": list(permissions)}
    enabled = client.post(f"/v1/projects/{PROJECT}/plugins/{plugin}/enable", json=body, headers=headers)
    assert enabled.status_code == 200, enabled.text


def enable_ba(client, hub, run_as: str = OWNER) -> dict:
    """The admin enables the agent type ba under ``run_as``, with a cap of 5 USD a day."""
    typed = client.put(TYPE, json={"run_as": run_as, "daily_usd": 5}, headers=hub["owner"])
    assert typed.status_code == 200, typed.text
    return typed.json()


def owners_worker(client, hub, name: str = "studio", slots: int = 3) -> dict:
    """A worker of owner that runs chat and task runs and builds the packs of plugin srs."""
    worker = chat_worker(client, hub["owner"], name, slots=slots)
    assert set_plugins(client, hub["owner"], worker, ["srs"]).status_code == 200
    return worker


def imported(client, headers) -> dict:
    body = {"markdown": SAMPLE.read_text(encoding="utf-8"), "slug": "demo-shop"}
    answer = client.post(f"{SRS}/import", json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def item(client, headers, ref: str) -> dict:
    found = client.get(f"{DOC}/items/{ref}", headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def version(client, headers, number: int) -> dict:
    found = client.get(f"{DOC}/versions/{number}", headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def submitted(client, headers, number: int) -> dict:
    answer = client.post(f"{DOC}/versions/{number}/submit", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def review(client, headers, number: int, decision: str = "approve", **body):
    return client.post(f"{DOC}/versions/{number}/reviews", json={"decision": decision, **body}, headers=headers)


def delivered(client) -> dict:
    """One pass of the job hub.deliver_plugin_events, as the hub runs it every minute, none of whose deliveries fails:
    the event srs.version.approved of an approval reaches the plugin srs, whose handler keeps the tasks in step
    (``evo_agents.hub.server.srs.events``)."""
    report = client.portal.call(lambda: plugin_events.deliver(client.app.state.engine))
    assert (report["retried"], report["failed"]) == (0, 0), report
    return report


def saved(client, headers, base: int, *changes: dict) -> dict:
    answer = client.post(f"{DOC}/versions", json={"base_version": base, "changes": list(changes)}, headers=headers)
    assert answer.status_code == 201, answer.text
    return answer.json()


def comments(client, headers) -> list[dict]:
    listed = client.get(f"{DOC}/comments", headers=headers)
    assert listed.status_code == 200, listed.text
    return listed.json()


def agent_of(worker: dict, run_id: int) -> dict:
    """The headers of the agent of run ``run_id`` on the hub's /mcp: its worker's token and the run."""
    return {"Authorization": worker["headers"]["Authorization"], "X-Evo-Run": str(run_id)}


def tool(client, headers, name: str, project: str | None = None, **arguments) -> tuple[str, bool]:
    """(the text of the tool's answer, whether it is an error); ``project`` for a member's own session."""
    result = rpc(client, headers, call(9, name, arguments), project=project)
    return result["content"][0]["text"], result["isError"]


def mention(client, headers, chat_id: int, text: str, reply_to: int | None = None) -> tuple[dict, dict]:
    """(the message, its one mention)."""
    message = sent(client, headers, chat_id, text, reply_to)
    (found,) = message["mentions"]
    return message, found


def running(client, worker: dict, kind: str) -> dict:
    """The worker claims its next run, which is of ``kind``, and starts it: its spec."""
    spec = claim(client, worker)
    assert spec is not None and spec["kind"] == kind, spec
    moved(client, worker, spec["id"], "running", session_id=f"sess-{spec['id']}")
    return spec


def runs_of(db, kind: str) -> list[tuple]:
    """The runs of ``kind``: id, profile's handle, dispatcher's login, credential kind, title, by id."""
    r, u, p = tables.runs, tables.users, tables.agent_profiles
    query = (
        select(r.c.id, p.c.handle, u.c.login, r.c.dispatched_via, r.c.title)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .join(p, p.c.id == r.c.profile_id)
        .where(r.c.kind == kind)
    )
    return sql(db, query.order_by(r.c.id))


# The agent type and its tools


def test_ba_agent_type_makes_ba_with_the_pack_srs_ba_and_its_tools_show_only_where_srs_is_on(client, members):
    session = {"project": PROJECT}
    assert not [name for name in tool_names(client, members["other"], **session) if name.startswith("srs__")]
    assert client.put(TYPE, json={"run_as": OWNER, "daily_usd": 5}, headers=members["owner"]).status_code == 409
    enable(client, members["owner"], "srs")
    assert sorted(n for n in tool_names(client, members["other"], **session) if n.startswith("srs__")) == SRS_TOOLS
    assert "tasks__create" not in tool_names(client, members["other"], **session)
    refused = client.put(TYPE, json={"run_as": OWNER, "daily_usd": 5}, headers=members["other"])
    assert refused.status_code == 403, refused.text  # an admin of the project enables it
    typed = enable_ba(client, members)
    assert (typed["handle"], typed["enabled"], typed["run_as"], typed["packs"]) == ("ba", True, OWNER, ["srs/ba"])
    made = client.get(f"/v1/projects/{PROJECT}/agents/ba", headers=members["reader"]).json()
    assert (made["name"], made["instructions"]) == ("BA", BA_INSTRUCTIONS)
    assert made["packs"] == [
        {"pack": "srs/ba", "plugin": "srs", "name": "ba", "version": SRS_MANIFEST["version"]},
    ]
    enable(client, members["owner"], "tasks", ["plans:read"])
    assert "tasks__create" in tool_names(client, members["other"], **session)
    # a member's own session reads, and never proposes or comments
    imported(client, members["chi"])
    text, error = tool(client, members["other"], "srs__read", PROJECT)
    assert not error and "- demo-shop (srs): Demo Shop: Order Service; latest version 1 (draft)" in text, text
    for name, arguments in (
        ("srs__propose", {"document": "demo-shop", "base_version": 1, "summary": "x", "changes": [{"key": "k"}]}),
        ("srs__comment", {"document": "demo-shop", "body": "Suggestion: shorter"}),
        ("srs__finding", {"action": "reopen", "document": "demo-shop", "finding": 1}),
    ):
        text, error = tool(client, members["other"], name, PROJECT, **arguments)
        assert error and f"calls {name}" in text and "only the agent of" in text, text


# Where its chat runs go


def test_ba_agent_chat_runs_go_to_a_worker_of_run_as_even_when_the_writer_who_mentions_it_has_none(client, hub, hub_db):
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI, READER], agents=("ba", "reviewer"))
    asked, found = mention(client, hub["other"], chat["id"], "@ba add a retry limit to UC ORD-2")
    assert found["refusal"] is None, found
    run_id = found["run_id"]
    run = run_of(client, hub["chi"], run_id)
    assert (run["kind"], run["profile"], run["dispatched_by"], run["pinned_worker_id"]) == (
        "chat",
        "ba",
        OWNER,
        studio["id"],
    )
    assert runs_of(hub_db, "chat") == [(run_id, "ba", OWNER, "machine", run["title"])]
    spec = running(client, studio, "chat")
    assert (spec["id"], spec["profile"]["handle"]) == (run_id, "ba")
    assert [pack["pack"] for pack in spec["packs"]] == ["srs/ba"]
    assert f"- Work for {OTHER}, whose message" in spec["prompt"], "the run works for the writer who mentioned @ba"
    # the same writer's next mention in the thread reaches that session; another writer's starts one of theirs
    _, again = mention(client, hub["other"], chat["id"], "@ba and UC ORD-1 too", reply_to=asked["id"])
    assert (again["run_id"], again["refusal"]) == (run_id, None)
    _, theirs = mention(client, hub["chi"], chat["id"], "@ba check UC RET-1", reply_to=asked["id"])
    assert theirs["refusal"] is None and theirs["run_id"] != run_id
    assert run_of(client, hub["chi"], theirs["run_id"])["dispatched_by"] == OWNER
    # a reader's mention is refused, as for any agent
    _, read_only = mention(client, hub["reader"], chat["id"], "@ba what is UC ORD-1?")
    assert read_only["run_id"] is None and f"{READER} holds reader there" in read_only["refusal"]
    # an agent of no such type routes as before: to the mentioner's own worker, which someone-else lacks
    _, plain = mention(client, hub["other"], chat["id"], "@reviewer read the change")
    assert plain["run_id"] is None
    assert f"{OTHER} has no worker that takes runs of project {PROJECT}" in plain["refusal"]
    assert [row[0] for row in runs_of(hub_db, "chat")] == [run_id, theirs["run_id"]]


def test_ba_agent_routes_as_before_once_its_type_is_off_and_refuses_a_run_as_who_lost_writer(client, hub, hub_db):
    enable_ba(client, hub, run_as=CHI)
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    _, found = mention(client, hub["other"], chat["id"], "@ba draft UC ORD-3")
    assert found["run_id"] is None
    assert f"@ba runs on the workers of {CHI}, who has no worker" in found["refusal"], found
    granted(client, hub, CHI, READ_ONLY)
    _, found = mention(client, hub["other"], chat["id"], "@ba draft UC ORD-3")
    assert f"@ba runs on the workers of {CHI}, who holds reader on project {PROJECT} now" in found["refusal"]
    # turned off: @ba is an agent like any other, on the worker of the person who mentions it
    off = client.delete(TYPE, headers=hub["owner"])
    assert off.status_code == 200, off.text
    _, found = mention(client, hub["other"], chat["id"], "@ba draft UC ORD-3")
    assert f"{OTHER} has no worker that takes runs of project {PROJECT}" in found["refusal"], found
    mine = chat_worker(client, hub["other"], "desk")
    assert set_plugins(client, hub["other"], mine, ["srs"]).status_code == 200
    _, found = mention(client, hub["other"], chat["id"], "@ba draft UC ORD-3")
    assert found["refusal"] is None
    assert run_of(client, hub["other"], found["run_id"])["dispatched_by"] == OTHER
    assert runs_of(hub_db, "chat")[-1][1:3] == ("ba", OTHER)


# Proposals


def test_ba_agent_proposes_a_version_in_review_written_as_the_writer_who_asked_who_never_approves_it(
    client, hub, hub_db
):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    assert runs_of(hub_db, "task") == [], "no review by @ba while its type is not enabled"
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    _, found = mention(client, hub["other"], chat["id"], "@ba allow two payment retries, and add a notice rule")
    spec = running(client, studio, "chat")
    assert spec["id"] == found["run_id"]
    agent = agent_of(studio, spec["id"])
    assert sorted(name for name in tool_names(client, agent) if name.startswith("srs__")) == SRS_TOOLS
    # reading
    text, error = tool(client, agent, "srs__search", query="UC ORD-2")
    pay = item(client, hub["chi"], "UC ORD-2")
    assert not error and f"- demo-shop v1: UC ORD-2 (use_case) Pay for an order [key {pay['key']}]" in text, text
    retries = item(client, hub["chi"], "BR ORD2-1")
    text, error = tool(client, agent, "srs__read", document="demo-shop", item="UC ORD-2")
    assert not error and f"Item UC ORD-2 (use_case), key {pay['key']}" in text
    assert f"BR ORD2-1 (business_rule) Payment retries [key {retries['key']}]" in text
    # a change of code, a drop, or a code another item holds is refused, and nothing is written
    for change in ({"key": pay["key"], "code": "UC ORD-9"}, {"key": retries["key"], "retire": True}):
        text, error = tool(
            client, agent, "srs__propose", document="demo-shop", base_version=1, summary="x", changes=[change]
        )
        assert error and text.startswith("error: bad arguments for srs__propose"), text
    taken = {"new": {"kind": "use_case", "code": "UC ORD-1", "title": "Again"}}
    text, error = tool(
        client, agent, "srs__propose", document="demo-shop", base_version=1, summary="x", changes=[taken]
    )
    assert error and text.startswith("error: the version is not saved") and "UC ORD-1" in text, text
    assert client.get(DOC, headers=hub["chi"]).json()["latest_version"] == 1
    # a proposal is the next version, in review at once, written as the writer who asked and drafted by the run
    body = retries["body"].replace("retried 3 times", "retried 2 times")
    notice = {"kind": "business_rule", "code": "BR ORD2-2", "title": "Retry notice", "parent": pay["key"]}
    notice |= {"body": "- Each failed payment says how many tries are left.", "steps": ["2.1"]}
    text, error = tool(
        client,
        agent,
        "srs__propose",
        document="demo-shop",
        base_version=1,
        summary="Two payment retries, as someone-else asked",
        changes=[{"key": retries["key"], "body": body}, {"new": notice}],
    )
    assert not error, text
    (review_run,) = [row[0] for row in runs_of(hub_db, "task")]
    assert text.startswith(
        f"Version 2 of demo-shop is in review, drafted by run #{spec['id']} and written as {OTHER}, who therefore "
        "does not approve it"
    ), text
    assert f"/p/{PROJECT}/x/srs/documents/demo-shop/compare?from=1&to=2" in text
    assert f"@ba reviews it in run #{review_run}" in text
    assert "## changed: BR ORD2-1" in text and "## added: BR ORD2-2" in text
    proposed = version(client, hub["reader"], 2)
    assert (proposed["state"], proposed["author"], proposed["drafted_by_run"]) == ("in_review", OTHER, spec["id"])
    assert proposed["summary"] == "Two payment retries, as someone-else asked"
    text, error = tool(client, agent, "srs__diff", document="demo-shop", **{"from": 1, "to": 2})
    assert not error and text.startswith("demo-shop: version 1 to version 2, 2 item(s) changed."), text
    # nobody approves or withdraws it through the run, and the writer who asked does not approve it either
    assert review(client, agent, 2).status_code in (401, 403)
    assert client.post(f"{DOC}/versions/2/withdraw", headers=agent).status_code in (401, 403)
    author = review(client, hub["other"], 2)
    assert author.status_code == 403, author.text
    held = review(client, hub["chi"], 2)  # @ba's review of the round is still queued
    assert held.status_code == 409 and f"(run #{review_run}) is queued" in held.json()["message"], held.text
    moved(client, studio, running(client, studio, "task")["id"], "verifying", "done", summary="Nothing found.")
    approved = review(client, hub["chi"], 2)
    assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
    assert "BR ORD2-2" in {entry["code"] for entry in version(client, hub["reader"], 2)["items"]}


def test_ba_agent_proposal_with_self_approval_on_is_approved_by_the_writer_who_asked(client, hub):
    imported(client, hub["chi"])
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    on = {"values": {"self_approval": True}}
    written = client.post(f"/v1/projects/{PROJECT}/plugins/srs/config", json=on, headers=hub["owner"])
    assert written.status_code == 200, written.text
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    mention(client, hub["other"], chat["id"], "@ba rename UC ORD-1")
    spec = running(client, studio, "chat")
    agent = agent_of(studio, spec["id"])
    order = item(client, hub["chi"], "UC ORD-1")
    change = {"key": order["key"], "title": "Place an order now"}
    text, error = tool(
        client, agent, "srs__propose", document="demo-shop", base_version=1, summary="Rename", changes=[change]
    )
    assert not error, text
    assert text.startswith(
        f"Version 2 of demo-shop is in review, drafted by run #{spec['id']} and written as {OTHER}. Self-approval is "
        f"on in project {PROJECT}, so {OTHER} may approve it as any writer may."
    ), text
    assert review(client, agent, 2).status_code in (401, 403), "the agent of a run never approves, switch or not"
    held = review(client, hub["other"], 2)  # the author may review it, once @ba's review of the round ends
    assert held.status_code == 409 and "is queued" in held.json()["message"], held.text
    moved(client, studio, running(client, studio, "task")["id"], "verifying", "done", summary="Nothing found.")
    approved = review(client, hub["other"], 2)
    assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text


def test_ba_agent_proposes_only_in_a_chat_run_of_ba(client, hub):
    imported(client, hub["chi"])
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    chat = created(client, hub["owner"], "SRS", people=[OTHER], agents=("ba", "reviewer"))
    mention(client, hub["owner"], chat["id"], "@reviewer tidy UC ORD-1")
    spec = running(client, studio, "chat")
    assert spec["profile"]["handle"] == "reviewer"
    agent = agent_of(studio, spec["id"])
    order = item(client, hub["chi"], "UC ORD-1")
    change = {"key": order["key"], "title": "Place an order now"}
    text, error = tool(
        client, agent, "srs__propose", document="demo-shop", base_version=1, summary="x", changes=[change]
    )
    assert error and "only the agent of a chat run of @ba" in text, text
    text, error = tool(client, agent, "srs__read", document="demo-shop", item="UC ORD-1")
    assert not error and "Item UC ORD-1 (use_case)" in text, "reading is anyone's with a grant"


def test_ba_agent_leaves_out_an_archived_document_reads_it_by_its_slug_and_writes_nothing_in_it(client, hub):
    imported(client, hub["chi"])
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    mention(client, hub["other"], chat["id"], "@ba tidy UC ORD-1")
    agent = agent_of(studio, running(client, studio, "chat")["id"])
    assert client.post(f"{DOC}/archive", headers=agent).status_code in (401, 403)
    archived = client.post(f"{DOC}/archive", headers=hub["owner"])
    assert archived.status_code == 200, archived.text
    text, error = tool(client, agent, "srs__read")
    assert not error and text == f"Project {PROJECT} has no SRS document that {OTHER} sees.", text
    text, error = tool(client, agent, "srs__search", query="UC ORD-1")
    assert not error and text == "No item of the latest versions matches 'UC ORD-1'.", text
    text, error = tool(client, agent, "srs__read", document="demo-shop", item="UC ORD-1")
    assert not error and "Item UC ORD-1 (use_case)" in text, text
    order = item(client, hub["chi"], "UC ORD-1")
    change = {"key": order["key"], "title": "Place an order now"}
    for name, arguments in (
        ("srs__propose", {"document": "demo-shop", "base_version": 1, "summary": "x", "changes": [change]}),
        ("srs__comment", {"document": "demo-shop", "version": 1, "body": "Suggestion: shorter"}),
    ):
        text, error = tool(client, agent, name, **arguments)
        assert error and "demo-shop" in text and "is archived and takes no change" in text, text
        assert f"`evo-agents hub srs unarchive demo-shop --project {PROJECT}`" in text, text
    assert client.get(DOC, headers=hub["chi"]).json()["latest_version"] == 1
    assert comments(client, hub["reader"]) == []
    assert client.post(f"{DOC}/unarchive", headers=hub["owner"]).status_code == 200
    text, error = tool(client, agent, "srs__search", query="UC ORD-1")
    assert not error and f"- demo-shop v1: UC ORD-1 (use_case) Place an order [key {order['key']}]" in text, text


# Its review of each version


def test_ba_agent_reviews_a_version_each_time_it_enters_review_and_its_comments_block_nothing(client, hub, hub_db):
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    assert submitted(client, hub["chi"], 1)["state"] == "in_review"
    ((review_run, handle, dispatcher, via, title),) = runs_of(hub_db, "task")
    assert (handle, dispatcher, via, title) == ("ba", OWNER, "machine", "@ba reviews demo-shop v1")
    spec = running(client, studio, "task")
    assert (spec["id"], spec["mode"], [pack["pack"] for pack in spec["packs"]]) == (review_run, "headless", ["srs/ba"])
    assert "Review version 1 of the SRS document demo-shop" in spec["prompt"]
    assert "Follow the skill srs-review of your pack" in spec["prompt"]
    for said in (
        "every use case of every document",
        "with the glossary",
        "non-functional requirements",
        "srs__finding",
    ):
        assert said in spec["prompt"], said
    assert "Comments are advice: they block no approval." in spec["prompt"]
    agent = agent_of(studio, review_run)
    finding = "Blocking: BR ORD2-1 lets 3 retries run past the 15 minute hold of BR ORD1-2."
    text, error = tool(client, agent, "srs__comment", document="demo-shop", version=1, item="BR ORD2-1", body=finding)
    assert not error and text.endswith(f"of demo-shop, as {OWNER}, drafted by run #{review_run}."), text
    assert [
        (c["author"], c["run_id"], c["item_code"], c["version"], c["body"]) for c in comments(client, hub["reader"])
    ] == [(OWNER, review_run, "BR ORD2-1", 1, finding)]
    # its review proposes nothing, makes no task, and its comments block no approval
    order = item(client, hub["chi"], "UC ORD-1")
    change = {"key": order["key"], "title": "Place an order now"}
    text, error = tool(
        client, agent, "srs__propose", document="demo-shop", base_version=1, summary="x", changes=[change]
    )
    assert error and "only the agent of a chat run of @ba" in text, text
    enable(client, hub["owner"], "tasks", ["plans:read"])
    text, error = tool(client, agent, "tasks__create", title="Build Place an order", links=["UC ORD-1"])
    assert error and "the agent of a task run creates no task" in text, text
    held = review(client, hub["other"], 1)  # approval waits for the review run, never for its comments
    assert held.status_code == 409 and f"(run #{review_run}) is running" in held.json()["message"], held.text
    moved(client, studio, review_run, "verifying", "done", summary="One finding.")
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    # with tasks on, the approval hands @ba the split of its frame tasks too, once its event is delivered
    # (tests.hub.test_srs_auto_tasks)
    delivered(client)
    split = running(client, studio, "task")
    assert "Split the frame tasks of the SRS document demo-shop" in split["prompt"]
    moved(client, studio, split["id"], "verifying", "done", summary="Nothing split.")
    # a version sent back and submitted again is reviewed again, in its new round
    cart = item(client, hub["chi"], "BR ORD1-1")
    saved(client, hub["other"], 1, {"key": cart["key"], "body": "- A cart holds at most 40 lines."})
    submitted(client, hub["other"], 2)
    moved(client, studio, running(client, studio, "task")["id"], "verifying", "done", summary="Nothing found.")
    returned = review(client, hub["chi"], 2, "request_changes", body="Say why 40")
    assert returned.json()["state"] == "draft", returned.text
    assert submitted(client, hub["other"], 2)["round"] == 2
    assert [row[4] for row in runs_of(hub_db, "task")] == [
        "@ba reviews demo-shop v1",
        "@ba splits the tasks of demo-shop v1",
        "@ba reviews demo-shop v2",
        "@ba reviews demo-shop v2, round 2",
    ]
    again = running(client, studio, "task")
    assert "submitted once more" in again["prompt"] and "(round 2)" in again["prompt"], again["prompt"]
    moved(client, studio, again["id"], "verifying", "done", summary="Nothing found.")
    # with the type off, a version goes to review all the same, without a review by @ba
    assert review(client, hub["chi"], 2).json()["state"] == "approved"
    delivered(client)  # the approval's event, while the type is on
    stock = item(client, hub["chi"], "BR ORD1-2")
    saved(client, hub["other"], 2, {"key": stock["key"], "body": "- Placing the order holds its stock for 20 minutes."})
    assert client.delete(TYPE, headers=hub["owner"]).status_code == 200
    assert submitted(client, hub["other"], 3)["state"] == "in_review"
    assert len(runs_of(hub_db, "task")) == 5  # the approval of version 2 handed the split of the frames still waiting


# Tasks of its use cases


def test_ba_agent_makes_a_task_of_a_use_case_for_the_writer_who_asked_while_they_hold_writer(client, hub, hub_db):
    imported(client, hub["chi"])
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    mention(client, hub["other"], chat["id"], "@ba make a task of UC ORD-1")
    spec = running(client, studio, "chat")
    agent = agent_of(studio, spec["id"])
    task = {"title": "Build Place an order", "description_md": "Checks BR ORD1-1 and BR ORD1-2.", "links": ["UC ORD-1"]}
    text, error = tool(client, agent, "tasks__create", **task)
    assert error and "no tool tasks__create in project" in text, "the project has not enabled tasks"
    enable(client, hub["owner"], "tasks", ["plans:read"])
    text, error = tool(client, agent, "tasks__create", **task)
    assert not error and text.startswith(f"Task T-1 created for {OTHER}, linked to UC ORD-1: Build Place an order"), (
        text
    )
    made = client.get(f"/v1/x/tasks/{PROJECT}/tasks/T-1", headers=hub["reader"]).json()
    assert (made["created_by"], made["codes"], made["assignees"]) == (OTHER, ["UC ORD-1"], [])
    # once the writer who asked is a reader, @ba makes no task for them
    granted(client, hub, OTHER, READ_ONLY)
    text, error = tool(client, agent, "tasks__create", **task)
    assert error and "needs the writer role" in text, text
    assert len(client.get(f"/v1/x/tasks/{PROJECT}/tasks", headers=hub["reader"]).json()) == 1
