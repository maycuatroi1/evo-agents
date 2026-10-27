"""The core plugin builder on the hub (``evo_agents.core_plugins.builder``), as criterion a12 of the plan
hub-agent-plugins names it: an admin of a project enables its agent type, which makes the profile @builder with the
pack builder/builder that the hub's image ships; a writer who mentions @builder gets the hub's refusal, and an admin's
mention makes a chat run whose claim hands the pack. The run's agent proposes a change of a setting with the tool
builder__propose of /mcp: the hub checks it, keeps it pending, shows it in the thread and tells the members who may
approve it, and applies nothing. An admin approves it, in the thread or with the plugin's routes, and the hub applies
it as the setting's next version while the setting is still where the proposal found it, or keeps why it could not;
an applied proposal rolls back to a copy of the version before. The agent that runs on the worker is a stand-in: the
test calls the tool with the worker's token and the run, as its MCP client would. The pack's files and the CI
template are ``tests.worker.test_builder``'s.
"""

from __future__ import annotations

import dataclasses
import re
from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.cli import build_parser
from evo_agents.hub import cli_intake, plugin_calls, plugin_events, tables
from evo_agents.hub.client import HubError
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chat_runs import chat_worker, created, ended, sent, thread
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_mcp import call
from tests.hub.test_plugin_points import install, on_hub, rpc, sample_manifest, tool_names
from tests.hub.test_plugin_runtimes import message, set_plugins
from tests.hub.test_runs import PROJECT, audit_rows, claim, moved

app_key = credentials_api.app_key  # a fixture
OWNER, OTHER, READER = credentials_api.OWNER, credentials_api.OTHER, "reader"
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
READ_ONLY = {"role": "reader", "max_level": "internal"}
BUILDER = EntryPoint("builder", "evo_agents.core_plugins.builder:MANIFEST", ENTRY_POINT_GROUP)
TOOL = "builder__propose"
PROPOSALS = f"/v1/x/builder/{PROJECT}/proposals"
WAITS = re.compile(r"Proposal (\d+) waits for ")


@pytest.fixture
def builder_core(monkeypatch):
    """The entry points of the core plugins with builder's, as the package installed declares it."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name != "builder"] + [BUILDER]
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3, builder_core):  # noqa: F811
    config = dataclasses.replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project of ``test_credentials_api``, owner its admin, someone-else a writer, reader and the hub admin
    readers; plugin builder enabled there, its agent type on under owner's name, and the agent @reviewer. The
    headers of each."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
        "reader": bearer(live.sign_in(client, github, READER, 504)["token"]),
    }
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    grants = ((OWNER, ADMIN_GRANT), (OTHER, credentials_api.WRITER), (READER, READ_ONLY), (ADMIN, READ_ONLY))
    for login, grant in grants:
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    enabled = client.post(
        f"/v1/projects/{PROJECT}/plugins/builder/enable", json={"permissions": ["chats:read"]}, headers=headers["owner"]
    )
    assert enabled.status_code == 200, enabled.text
    typed = client.put(
        f"/v1/projects/{PROJECT}/agent-types/builder/builder",
        json={"run_as": OWNER, "daily_usd": 5},
        headers=headers["owner"],
    )
    assert typed.status_code == 200, typed.text
    made = client.post(
        f"/v1/projects/{PROJECT}/agents",
        json={"handle": "reviewer", "name": "Reviewer", "instructions": "Read the diff."},
        headers=headers["owner"],
    )
    assert made.status_code == 201, made.text
    return headers


def owners_worker(client, hub) -> dict:
    """A worker of owner that runs chat runs and builds the packs of plugin builder."""
    worker = chat_worker(client, hub["owner"], "studio")
    assert set_plugins(client, hub["owner"], worker, ["builder"]).status_code == 200
    return worker


def started(client, hub, text: str = "@builder rename the hub and make it blue", worker: dict | None = None) -> dict:
    """Owner's mention of @builder in a chat with someone-else and the hub admin, its run claimed by owner's worker
    and running: the chat, the mention, the claim, and the headers of the run's agent on the hub's /mcp."""
    worker = owners_worker(client, hub) if worker is None else worker
    chat = created(client, hub["owner"], "Hub setup", people=[OTHER, ADMIN], agents=("builder",))
    mention = sent(client, hub["owner"], chat["id"], text)
    spec = claim(client, worker)
    assert spec is not None and spec["kind"] == "chat" and spec["profile"]["handle"] == "builder", spec
    moved(client, worker, spec["id"], "running", session_id="sess-1")
    agent = {"Authorization": worker["headers"]["Authorization"], "X-Evo-Run": str(spec["id"])}
    return {"chat": chat, "mention": mention, "spec": spec, "agent": agent, "worker": worker}


def proposed(client, agent: dict, **arguments) -> str:
    """The text of the tool's answer to the run's agent."""
    result = rpc(client, agent, call(5, TOOL, arguments), project=None)
    return result["content"][0]["text"]


def made_proposal(client, agent: dict, **arguments) -> int:
    text = proposed(client, agent, **arguments)
    found = WAITS.match(text)
    assert found is not None, text
    return int(found[1])


def answer(client, headers, proposal_id: int, verb: str):
    return client.post(f"{PROPOSALS}/{proposal_id}/answer", json={"answer": verb}, headers=headers)


def rollback(client, headers, proposal_id: int):
    return client.post(f"{PROPOSALS}/{proposal_id}/rollback", headers=headers)


def shown(client, headers, proposal_id: int) -> dict:
    response = client.get(f"{PROPOSALS}/{proposal_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def deliver(client) -> dict:
    """One pass of the event deliveries, with the hub's config, as the job does."""
    return on_hub(
        client,
        lambda app: plugin_events.deliver(app.state.engine, config=app.state.config, host=app.state.plugin_host),
    )


def said_after(client, headers, run: dict, before: int) -> list[str]:
    """The bodies of the hub's messages in the run's thread after the first ``before`` messages of it."""
    messages = thread(client, headers, run["chat"]["id"], run["mention"]["id"])[before:]
    return [item["body"] for item in messages if item["author"]["kind"] == "hub"]


def notices(db) -> list[tuple]:
    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.details["proposal"].as_integer(), n.c.details["kind"].as_string())
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == "builder_proposal")
        .order_by(n.c.id)
    )
    return sql(db, query)


def profile(client, headers, handle: str) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/agents/{handle}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# The agent type and its runs


def test_builder_is_a_core_plugin_whose_agent_type_takes_work_from_admins_alone_and_hands_its_pack(
    client, hub, hub_db, s3
):
    listed = {plugin["name"]: plugin for plugin in client.get("/v1/plugins", headers=hub["admin"]).json()}
    builder = listed["builder"]
    assert (builder["tier"], builder["state"], builder["installed_version"]) == ("core", "installed", "1.0.0")
    made = profile(client, hub["reader"], "builder")
    assert (made["name"], made["version"]) == ("Builder", 1)
    assert made["packs"] == [{"pack": "builder/builder", "plugin": "builder", "name": "builder", "version": "1.0.0"}]
    # a writer's mention is refused in the thread, with no run
    worker = chat_worker(client, hub["other"], "theirs")
    assert set_plugins(client, hub["other"], worker, ["builder"]).status_code == 200
    chat = created(client, hub["owner"], "Hub setup", people=[OTHER], agents=("builder",))
    refused = sent(client, hub["other"], chat["id"], "@builder turn the knowledge graph off")
    (refusal,) = [item["refusal"] for item in refused["mentions"]]
    assert refusal == (
        f"@builder: @builder takes work from the admins of project {PROJECT} alone, and {OTHER} holds writer there"
    )
    assert thread(client, hub["owner"], chat["id"], refused["id"])[-1]["body"] == refusal
    r = tables.runs
    assert sql(hub_db, select(func.count()).select_from(r).where(r.c.kind == "chat")) == [(0,)]
    # an admin's mention makes the run, and its claim hands the pack the image ships
    run = started(client, hub)
    (pack,) = run["spec"]["packs"]
    bundle = core.image_bundle(next(p for p in core.core_plugins() if p.manifest.id == "builder"))
    assert (pack["pack"], pack["version"], pack["sha256"], pack["size"]) == (
        "builder/builder",
        "1.0.0",
        bundle.sha256,
        len(bundle.data),
    )
    assert f"blobs/sha256/{bundle.sha256}" in pack["url"] and s3.bucket in pack["url"]
    assert TOOL in tool_names(client, run["agent"], project=None)


# Proposals of the project's admins


def test_builder_agent_proposal_waits_for_a_project_admin_then_applies_as_a_new_version_and_rolls_back(
    client, hub, hub_db
):
    run = started(client, hub)
    before = len(thread(client, hub["owner"], run["chat"]["id"], run["mention"]["id"]))
    proposal_id = made_proposal(
        client,
        run["agent"],
        kind="agent",
        summary="Ask @reviewer to name the failing test in each review",
        target={"agent": "reviewer"},
        set={"instructions": "Read the diff. Name the failing test."},
    )
    pending = shown(client, hub["reader"], proposal_id)
    assert (pending["state"], pending["kind"], pending["base_version"], pending["approver"]) == (
        "pending",
        "agent",
        1,
        "project_admin",
    )
    assert (pending["proposed_by"], pending["run_id"], pending["applied_version"]) == (OWNER, run["spec"]["id"], None)
    assert profile(client, hub["reader"], "reviewer")["instructions"] == "Read the diff.", "nothing is applied yet"
    (shown_in_thread,) = said_after(client, hub["owner"], run, before)
    assert shown_in_thread.startswith(f"Proposal {proposal_id} of @builder: Ask @reviewer to name the failing test")
    assert f"reply `approve {proposal_id}` or `reject {proposal_id}`" in shown_in_thread
    assert notices(hub_db) == [(OWNER, proposal_id, "agent")]
    # neither a writer nor the run's agent decides it
    assert "needs the admin role on project" in message(answer(client, hub["other"], proposal_id, "approve"), 403)
    assert answer(client, run["agent"], proposal_id, "approve").status_code in (401, 403)
    assert shown(client, hub["reader"], proposal_id)["state"] == "pending"
    # owner approves it: the profile's next version
    approved = answer(client, hub["owner"], proposal_id, "approve")
    assert approved.status_code == 200, approved.text
    assert (approved.json()["state"], approved.json()["applied_version"], approved.json()["decided_by"]) == (
        "applied",
        2,
        OWNER,
    )
    after = profile(client, hub["reader"], "reviewer")
    assert (after["version"], after["instructions"]) == (2, "Read the diff. Name the failing test.")
    assert "is applied already" in message(answer(client, hub["owner"], proposal_id, "approve"), 409)
    # rolled back: a third version, a copy of the first
    back = rollback(client, hub["owner"], proposal_id)
    assert back.status_code == 200, back.text
    assert (back.json()["state"], back.json()["rollback_version"]) == ("rolled_back", 3)
    again = profile(client, hub["reader"], "reviewer")
    assert (again["version"], again["instructions"]) == (3, "Read the diff.")
    assert said_after(client, hub["owner"], run, before)[1:] == [
        f"Proposal {proposal_id} is applied by {OWNER}: the setting is at version 2 now. "
        f"`rollback {proposal_id}` brings back the version before it.",
        f"Proposal {proposal_id} is rolled back: version 3 of the setting is a copy of the one before version 2.",
    ]
    assert [row[:2] for row in audit_rows(hub_db, "builder")] == [
        ("builder.propose", f"{PROJECT}/builder:proposal:{proposal_id} run:{run['spec']['id']}"),
        ("builder.decide", f"{PROJECT}/builder:proposal:{proposal_id} approve state=applied"),
        ("builder.decide", f"{PROJECT}/builder:proposal:{proposal_id} rollback state=rolled_back"),
    ]
    assert [row[2] for row in audit_rows(hub_db, "builder")] == [OWNER, OWNER, OWNER]


def test_builder_plugin_config_proposal_fails_when_the_layer_moved_and_never_sets_a_secret(client, hub, hub_db):
    install(hub_db, sample_manifest(permissions=["runs:read"]))
    enabled = client.post(
        f"/v1/projects/{PROJECT}/plugins/sample/enable", json={"permissions": ["runs:read"]}, headers=hub["owner"]
    )
    assert enabled.status_code == 200, enabled.text
    run = started(client, hub, "@builder greet in French")
    config = f"/v1/projects/{PROJECT}/plugins/sample/config"
    target = {"plugin": "sample"}
    secret = proposed(client, run["agent"], kind="plugin_config", summary="key", target=target, set={"api_key": "x"})
    assert secret.startswith("error: api_key: a secret, which @builder never sets")
    wrong = proposed(
        client, run["agent"], kind="plugin_config", summary="long", target=target, set={"greeting": "x" * 41}
    )
    assert wrong.startswith("error: ") and wrong.endswith("; nothing changed")
    off = proposed(client, run["agent"], kind="plugin_config", summary="other", target={"plugin": "nope"}, set={"a": 1})
    assert off == f"error: plugin nope is not enabled in project {PROJECT}, or it is turned off"
    first = made_proposal(
        client,
        run["agent"],
        kind="plugin_config",
        summary="Greet in French",
        target=target,
        set={"greeting": "bonjour"},
    )
    # the layer moves before the approval: the proposal fails, saying why, and changes nothing
    written = client.post(config, json={"values": {"greeting": "hi"}}, headers=hub["owner"])
    assert written.status_code == 200, written.text
    failed = answer(client, hub["owner"], first, "approve")
    assert failed.status_code == 200, failed.text
    assert failed.json()["state"] == "failed"
    assert failed.json()["error"] == (
        f"the config of plugin sample in project {PROJECT} is at version 1 now, not 0: it changed since the proposal, "
        "so propose the change again; nothing changed"
    )
    assert client.get(config, headers=hub["owner"]).json()["values"] == {"greeting": "hi"}
    # proposed again, approved, rolled back
    second = made_proposal(
        client,
        run["agent"],
        kind="plugin_config",
        summary="Greet in French",
        target=target,
        set={"greeting": "bonjour"},
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    assert answer(client, hub["owner"], second, "approve").json()["applied_version"] == 2
    layer = client.get(config, headers=hub["owner"]).json()
    assert (layer["version"], layer["values"], layer["changed_by"]) == (2, {"greeting": "bonjour"}, OWNER)
    assert rollback(client, hub["owner"], second).json()["rollback_version"] == 3
    layer = client.get(config, headers=hub["owner"]).json()
    assert (layer["version"], layer["values"], layer["rolled_back_to"]) == (3, {"greeting": "hi"}, 1)
    assert "only an applied one rolls back" in message(rollback(client, hub["owner"], first), 409)


# Proposals of a change of the autonomy policy


def policy(client, headers) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/policy", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_builder_proposal_of_the_autonomy_policy_writes_its_next_revision_and_rolls_back(client, hub, hub_db):
    run = started(client, hub, "@builder let the hub merge the docs")
    docs = "lumen-kb-docs"  # on GitLab; evo-agents and lumen-identity are on github.com
    for arguments, said in (
        ({"set": {"merge.max_diff_lines": 500}}, "merge.max_diff_lines: the policy has no such key; nothing changed"),
        ({"set": {"repos.evo-agents.mode": "auto"}}, "repos.evo-agents.mode: evo-agents is on github.com, where "),
        ({"set": {"merge.hold_until": "soon"}}, 'merge.hold_until: one of merged, uat_check, prod_check, not "soon"'),
        ({"unset": ["judge.worker"]}, "judge.worker: the layer does not set it, so there is nothing to unset"),
        ({"set": {"repos.ghost.mode": "auto"}}, "repos.ghost: the project has no repo ghost"),
        ({"target": {"agent": "reviewer"}, "set": {"merge.hold_until": "merged"}}, "the target of a policy proposal"),
    ):
        text = proposed(client, run["agent"], kind="policy", summary="Merge the docs", **arguments)
        assert text.startswith(f"error: {said}"), text
    sets = {f"repos.{docs}.mode": "auto", f"repos.{docs}.target_branch": "dev", "merge.hold_until": "uat_check"}
    first = made_proposal(client, run["agent"], kind="policy", summary="Let the hub merge the docs repo", set=sets)
    pending = shown(client, hub["reader"], first)
    assert (pending["state"], pending["kind"], pending["base_version"], pending["approver"], pending["target"]) == (
        "pending",
        "policy",
        0,
        "project_admin",
        {},
    )
    assert policy(client, hub["reader"])["revision"] == 0, "nothing is applied yet"
    assert notices(hub_db) == [(OWNER, first, "policy")]
    # a writer does not decide it; the project's admin approves it as the policy's first revision
    assert "needs the admin role on project" in message(answer(client, hub["other"], first, "approve"), 403)
    approved = answer(client, hub["owner"], first, "approve")
    assert (approved.json()["state"], approved.json()["applied_version"]) == ("applied", 1), approved.text
    now = policy(client, hub["reader"])
    made = {"repos": {docs: {"mode": "auto", "target_branch": "dev"}}, "merge": {"hold_until": "uat_check"}}
    assert (now["revision"], now["policy"], now["created_by"]) == (1, made, OWNER)
    assert now["note"] == f"proposal {first} of @builder: Let the hub merge the docs repo"
    # the policy moves past a pending proposal: approved, it fails and changes nothing
    second = made_proposal(
        client,
        run["agent"],
        kind="policy",
        summary="Hold each merge for its PROD check",
        set={"merge.hold_until": "prod_check"},
        unset=[f"repos.{docs}.target_branch"],
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    by_hand = {**made, "audit": {"sample_rate": 0.2}}
    written = client.put(f"/v1/projects/{PROJECT}/policy", json={"policy": by_hand}, headers=hub["owner"])
    assert written.status_code == 200, written.text
    failed = answer(client, hub["owner"], second, "approve").json()
    assert (failed["state"], failed["error"]) == (
        "failed",
        f"the autonomy policy of project {PROJECT} is at version 2 now, not 1: it changed since the proposal, so "
        "propose the change again; nothing changed",
    )
    assert "it changed since the proposal was applied" in message(rollback(client, hub["owner"], first), 409)
    # one applied and rolled back: the next revision copies the one before it
    third = made_proposal(
        client, run["agent"], kind="policy", summary="Trip sooner", set={"breaker.judge_misses_7d": 1}
    )
    assert answer(client, hub["owner"], third, "approve").json()["applied_version"] == 3
    back = rollback(client, hub["owner"], third)
    assert (back.json()["state"], back.json()["rollback_version"]) == ("rolled_back", 4), back.text
    after = policy(client, hub["reader"])
    assert (after["revision"], after["policy"]) == (4, by_hand)
    assert after["note"] == f"rollback of proposal {third} of @builder: a copy of revision 2"
    keys = f"repos.{docs}.mode,repos.{docs}.target_branch,merge.hold_until"
    assert audit_rows(hub_db, "policy") == [
        ("policy.set", f"{PROJECT}/policy revision=1 keys={keys} proposal={first}", OWNER, PROJECT),
        ("policy.set", f"{PROJECT}/policy revision=2 keys=audit.sample_rate", OWNER, PROJECT),
        ("policy.set", f"{PROJECT}/policy revision=3 keys=breaker.judge_misses_7d proposal={third}", OWNER, PROJECT),
        (
            "policy.set",
            f"{PROJECT}/policy revision=4 keys=breaker.judge_misses_7d proposal={third} copy-of=2",
            OWNER,
            PROJECT,
        ),
    ]


# Proposals of a goal


def goal(client, headers, ref: str) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/goals/{ref}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_builder_proposal_of_a_goal_makes_it_writes_its_next_revision_and_rolls_back(client, hub, hub_db):
    run = started(client, hub, "@builder add a goal for the search")
    for arguments, said in (
        ({"set": {"statement": "x", "budget_usd": 5}}, "budget_usd: a goal has no such field; nothing changed"),
        ({"set": {"rank": 2}}, "statement: Field required; nothing changed"),  # a new goal needs a statement
        ({"set": {"rank": 2}, "unset": ["statement"]}, "statement: a goal always has one"),
        ({"target": {"goal": "G-4"}, "set": {"statement": "x"}}, f"project {PROJECT} has no goal G-4; a new goal"),
        ({"target": {"goal": "Search"}, "set": {"statement": "x"}}, "bad arguments for builder__propose: target.goal"),
        (
            {"set": {"statement": "x", "owner": READER}},
            f"owner: {READER} holds reader on project {PROJECT}; a goal's owner holds the writer role or above",
        ),
    ):
        text = proposed(client, run["agent"], kind="goal", summary="A goal for the search", **arguments)
        assert text.startswith(f"error: {said}"), text
    sets = {"statement": "Fewer searches find nothing", "deadline": "2026-12-31", "owner": OTHER}
    first = made_proposal(client, run["agent"], kind="goal", summary="A goal for the search", set=sets)
    pending = shown(client, hub["reader"], first)
    assert (pending["state"], pending["kind"], pending["base_version"], pending["approver"], pending["target"]) == (
        "pending",
        "goal",
        0,
        "project_admin",
        {"goal": "G-1"},  # the code the hub picked for the new goal
    )
    assert client.get(f"/v1/projects/{PROJECT}/goals", headers=hub["reader"]).json() == [], "nothing is applied yet"
    assert notices(hub_db) == [(OWNER, first, "goal")]
    # a writer does not decide it; the project's admin approves it as the goal's first revision
    assert "needs the admin role on project" in message(answer(client, hub["other"], first, "approve"), 403)
    approved = answer(client, hub["owner"], first, "approve")
    assert (approved.json()["state"], approved.json()["applied_version"]) == ("applied", 1), approved.text
    made = goal(client, hub["reader"], "G-1")
    assert (made["statement"], made["deadline"], made["owner"], made["rank"], made["created_by"]) == (
        "Fewer searches find nothing",
        "2026-12-31",
        OTHER,
        1,
        OWNER,
    )
    assert made["note"] == f"proposal {first} of @builder: A goal for the search"
    # a change of it, applied and rolled back to a copy of the revision before
    second = made_proposal(
        client,
        run["agent"],
        kind="goal",
        summary="Pause the search goal",
        target={"goal": "G-1"},
        set={"status": "paused", "rank": 3},
        unset=["deadline"],
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    assert answer(client, hub["owner"], second, "approve").json()["applied_version"] == 2
    paused = goal(client, hub["reader"], "G-1")
    assert (paused["status"], paused["rank"], paused["deadline"]) == ("paused", 3, None)
    same = proposed(client, run["agent"], kind="goal", summary="Pause", target={"goal": "G-1"}, set={"rank": 3})
    assert same.startswith(f"error: goal G-1 of project {PROJECT} has these values already"), same
    back = rollback(client, hub["owner"], second)
    assert (back.json()["state"], back.json()["rollback_version"]) == ("rolled_back", 3), back.text
    again = goal(client, hub["reader"], "G-1")
    assert (again["revision"], again["status"], again["rank"], again["deadline"]) == (3, "active", 1, "2026-12-31")
    assert again["note"] == f"rollback of proposal {second} of @builder: a copy of revision 1"
    assert "it changed since the proposal was applied" in message(rollback(client, hub["owner"], first), 409)
    # a new goal the proposal made is closed when it rolls back
    third = made_proposal(client, run["agent"], kind="goal", summary="Tickets", set={"statement": "Answer in a day"})
    assert answer(client, hub["owner"], third, "approve").json()["applied_version"] == 1
    assert rollback(client, hub["owner"], third).json()["rollback_version"] == 2
    dropped = goal(client, hub["reader"], "G-2")
    assert (dropped["status"], dropped["close_reason"]) == ("closed", "dropped")
    assert audit_rows(hub_db, "goal") == [
        ("goal.set", f"{PROJECT}/goal G-1 revision=1 fields=statement,deadline,rank,owner,status proposal={first}")
        + (OWNER, PROJECT),
        ("goal.set", f"{PROJECT}/goal G-1 revision=2 fields=deadline,rank,status proposal={second}", OWNER, PROJECT),
        (
            "goal.set",
            f"{PROJECT}/goal G-1 revision=3 fields=deadline,rank,status proposal={second} copy-of=1",
            OWNER,
            PROJECT,
        ),
        ("goal.set", f"{PROJECT}/goal G-2 revision=1 fields=statement,rank,owner,status proposal={third}")
        + (OWNER, PROJECT),
        (
            "goal.set",
            f"{PROJECT}/goal G-2 revision=2 fields=status,close_reason proposal={third} copy-of=0",
            OWNER,
            PROJECT,
        ),
    ]


# Proposals of a problem (plan problem-oriented-hub, criterion a1), and of the problem a goal answers (a2)

PROBLEM = {
    "statement": "Searches find nothing",
    "who": "shoppers of lumen",
    "when": "a search names a brand the shop sells under another name",
    "workaround": "they leave, or ask support",
}


def problem(client, headers, ref: str) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/problems/{ref}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_builder_proposal_of_a_problem_waits_for_a_writer_makes_it_writes_its_next_revision_and_rolls_back(
    client, hub, hub_db
):
    run = started(client, hub, "@builder note the problem of the search")
    for arguments, said in (
        ({"set": {**PROBLEM, "budget_usd": 5}}, "budget_usd: a problem has no such field; nothing changed"),
        ({"set": {"statement": "x"}}, "who: Field required"),  # a new problem says who, when and the workaround
        ({"set": {"rank": 2}, "unset": ["who"]}, "who: a problem always has one"),
        ({"target": {"problem": "P-4"}, "set": PROBLEM}, f"project {PROJECT} has no problem P-4; a new problem"),
        ({"target": {"problem": "p-1"}, "set": PROBLEM}, "bad arguments for builder__propose: target.problem"),
        ({"set": {**PROBLEM, "metric": {"key": "run.failed_rate", "aggregate": "mean", "target": 1}}}, "metric.target"),
        (
            {"set": {**PROBLEM, "status": "solved"}},
            "problem: Value error, a solved or dropped problem has a close_reason",
        ),
        (
            {"set": {**PROBLEM, "owner": READER}},
            f"owner: {READER} holds reader on project {PROJECT}; a problem's owner holds the writer role or above",
        ),
    ):
        text = proposed(client, run["agent"], kind="problem", summary="The search finds nothing", **arguments)
        assert text.startswith(f"error: {said}"), text
    first = made_proposal(client, run["agent"], kind="problem", summary="The search finds nothing", set=PROBLEM)
    pending = shown(client, hub["reader"], first)
    assert (pending["state"], pending["kind"], pending["base_version"], pending["approver"], pending["target"]) == (
        "pending",
        "problem",
        0,
        "project_writer",
        {"problem": "P-1"},  # the code the hub picked for the new problem
    )
    assert client.get(f"/v1/projects/{PROJECT}/problems", headers=hub["reader"]).json() == [], "nothing applied yet"
    assert notices(hub_db) == [(OWNER, first, "problem"), (OTHER, first, "problem")]  # its writers and admins
    # a reader does not decide it; a writer approves it as the problem's first revision
    assert "needs the writer role on project" in message(answer(client, hub["reader"], first, "approve"), 403)
    approved = answer(client, hub["other"], first, "approve")
    assert (approved.json()["state"], approved.json()["applied_version"]) == ("applied", 1), approved.text
    made = problem(client, hub["reader"], "P-1")
    assert (made["statement"], made["who"], made["owner"], made["created_by"], made["status"]) == (
        "Searches find nothing",
        "shoppers of lumen",
        OTHER,
        OTHER,
        "open",
    )
    assert made["note"] == f"proposal {first} of @builder: The search finds nothing"
    # a change of it, applied and rolled back to a copy of the revision before
    second = made_proposal(
        client,
        run["agent"],
        kind="problem",
        summary="The synonyms solved it",
        target={"problem": "P-1"},
        set={"status": "solved", "close_reason": "the search reads synonyms"},
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    assert answer(client, hub["owner"], second, "approve").json()["applied_version"] == 2
    solved = problem(client, hub["reader"], "P-1")
    assert (solved["status"], solved["close_reason"]) == ("solved", "the search reads synonyms")
    back = rollback(client, hub["other"], second)
    assert (back.json()["state"], back.json()["rollback_version"]) == ("rolled_back", 3), back.text
    again = problem(client, hub["reader"], "P-1")
    assert (again["revision"], again["status"], again["close_reason"]) == (3, "open", None)
    assert again["note"] == f"rollback of proposal {second} of @builder: a copy of revision 1"
    # a new problem the proposal made is dropped when it rolls back
    third = made_proposal(
        client, run["agent"], kind="problem", summary="Tickets", set={**PROBLEM, "statement": "Tickets wait a week"}
    )
    assert answer(client, hub["other"], third, "approve").json()["applied_version"] == 1
    assert rollback(client, hub["other"], third).json()["rollback_version"] == 2
    dropped = problem(client, hub["reader"], "P-2")
    assert (dropped["status"], dropped["close_reason"]) == ("dropped", f"rollback of proposal {third} of @builder")
    assert [row[1] for row in audit_rows(hub_db, "problem")] == [
        f"{PROJECT}/problem P-1 revision=1 fields=statement,who,when,workaround,rank,owner,status proposal={first}",
        f"{PROJECT}/problem P-1 revision=2 fields=status,close_reason proposal={second}",
        f"{PROJECT}/problem P-1 revision=3 fields=status,close_reason proposal={second} copy-of=1",
        f"{PROJECT}/problem P-2 revision=1 fields=statement,who,when,workaround,rank,owner,status proposal={third}",
        f"{PROJECT}/problem P-2 revision=2 fields=status,close_reason proposal={third} copy-of=0",
    ]
    # a goal proposal names the problem it answers, one that is not dropped
    refused = proposed(client, run["agent"], kind="goal", summary="Tickets", set={"statement": "x", "problem": "P-2"})
    assert refused.startswith("error: problem: problem P-2 is dropped"), refused
    goal_id = made_proposal(
        client, run["agent"], kind="goal", summary="Search", set={"statement": "Fewer empty searches", "problem": "P-1"}
    )
    assert "needs the admin role on project" in message(answer(client, hub["other"], goal_id, "approve"), 403)
    assert answer(client, hub["owner"], goal_id, "approve").json()["state"] == "applied"
    assert goal(client, hub["reader"], "G-1")["problem"] == "P-1"
    assert [item["code"] for item in problem(client, hub["reader"], "P-1")["goals"]] == ["G-1"]
    cleared = made_proposal(
        client, run["agent"], kind="goal", summary="No problem", target={"goal": "G-1"}, unset=["problem"]
    )
    assert answer(client, hub["owner"], cleared, "approve").json()["applied_version"] == 2
    assert goal(client, hub["reader"], "G-1")["problem"] is None


def lines_of(client, headers, ref: str) -> list[dict]:
    response = client.get(f"/v1/projects/{PROJECT}/problems/{ref}/evidence", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_builder_proposal_of_a_problem_carries_evidence_made_with_it_or_alone_and_removed_on_rollback(client, hub):
    run = started(client, hub, "@builder note the problem of the search")
    line = {
        "source_kind": "hub_chat",
        "source_uri": "hub://lumen/chats/3",
        "locator": "message 9",
        "speaker": "Mira",
        "excerpt": "Searches for a brand under its old name find nothing.",
        "signal": "complaint",
        "label": "internal",
    }
    for bad, said in (
        ({**line, "label": "personal"}, "evidence"),
        ({**line, "excerpt": "x" * 301}, "evidence"),
    ):
        text = proposed(client, run["agent"], kind="problem", summary="s", set=PROBLEM, evidence=[bad])
        assert text.startswith("error:") and said in text, text
    first = made_proposal(client, run["agent"], kind="problem", summary="Searches", set=PROBLEM, evidence=[line])
    duplicate = proposed(client, run["agent"], kind="problem", summary="Twice", set=PROBLEM, evidence=[line, line])
    assert duplicate.startswith("error:"), duplicate
    assert "evidence" in shown(client, hub["reader"], first)["change"]
    assert answer(client, hub["other"], first, "approve").json()["state"] == "applied"
    made = lines_of(client, hub["reader"], "P-1")
    assert [(e["code"], e["excerpt"], e["added_by"]) for e in made] == [("E-1", line["excerpt"], OTHER)]
    assert (
        problem(client, hub["reader"], "P-1")["evidence_count"],
        problem(client, hub["reader"], "P-1")["source_count"],
    ) == (1, 1)
    # a proposal of lines alone names the problem, writes no revision and adds its lines
    second = made_proposal(
        client,
        run["agent"],
        kind="problem",
        summary="More",
        target={"problem": "P-1"},
        evidence=[{**line, "source_uri": "hub://lumen/chats/4"}],
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    assert answer(client, hub["owner"], second, "approve").json()["applied_version"] == 1
    assert problem(client, hub["reader"], "P-1")["revision"] == 1
    assert [e["code"] for e in lines_of(client, hub["reader"], "P-1")] == ["E-1", "E-2"]
    assert rollback(client, hub["owner"], second).json()["state"] == "rolled_back"
    assert [e["code"] for e in lines_of(client, hub["reader"], "P-1")] == ["E-1"]
    no_target = proposed(client, run["agent"], kind="problem", summary="Alone", evidence=[line])
    assert no_target.startswith("error:"), no_target


# Proposals of a hub admin, answered in the thread


def test_builder_hub_wide_proposals_are_decided_by_a_hub_admin_in_the_thread(client, hub, hub_db):
    run = started(client, hub)
    colors = {"brand": "#0b5cad", "brand-hover": "#094a8c", "brand-soft": "#e6f0fa", "on-brand": "#ffffff"}
    renamed = made_proposal(
        client,
        run["agent"],
        kind="branding",
        summary="Name the hub Acme and make it blue",
        set={"name": "Acme hub", "colors": {"light": colors}},
    )
    knowledge = made_proposal(
        client,
        run["agent"],
        kind="features",
        summary="Turn the knowledge graph off in this project",
        target={"layer": "project"},
        set={"kg": False},
    )
    assert shown(client, hub["owner"], renamed)["approver"] == "hub_admin"
    assert notices(hub_db) == [(ADMIN, renamed, "branding"), (ADMIN, knowledge, "features")]
    deliver(client)
    before = len(thread(client, hub["owner"], run["chat"]["id"], run["mention"]["id"]))
    root = run["mention"]["id"]
    # the project's admin is no hub admin: the hub refuses in the thread
    sent(client, hub["owner"], run["chat"]["id"], f"approve {renamed}", reply_to=root)
    deliver(client)
    assert said_after(client, hub["owner"], run, before) == [
        f"{OWNER}: approving proposal {renamed} needs a hub admin, and {OWNER} is none"
    ]
    assert shown(client, hub["owner"], renamed)["state"] == "pending"
    assert client.get("/v1/branding").json()["name"] is None
    # the hub admin approves one and rejects the other, in the thread
    sent(client, hub["admin"], run["chat"]["id"], f"Approve #{renamed}.", reply_to=root)
    sent(client, hub["admin"], run["chat"]["id"], f"reject {knowledge}", reply_to=root)
    assert deliver(client)["delivered"] >= 2
    assert said_after(client, hub["owner"], run, before)[1:] == [
        f"Proposal {renamed} is applied by {ADMIN}: the setting is at version 1 now. "
        f"`rollback {renamed}` brings back the version before it.",
        f"Proposal {knowledge} is rejected by {ADMIN}: nothing changed.",
    ]
    branding = client.get("/v1/branding").json()
    assert (branding["name"], branding["colors"]["light"]) == ("Acme hub", colors)
    layer = client.get(f"/v1/admin/projects/{PROJECT}/features", headers=hub["admin"]).json()
    assert (layer["version"], layer["values"]) == (0, {})
    # a delivery made again finds each answer done, and says nothing
    d = tables.plugin_deliveries
    with live.engine(hub_db).begin() as conn:
        conn.execute(
            update(d).where(d.c.kind == "chat.message").values(state="pending", next_at=func.now(), delivered_at=None)
        )
    deliver(client)
    assert len(said_after(client, hub["owner"], run, before)) == 3
    # rolled back in the thread: the hub's layer of branding unsets what version 1 set
    sent(client, hub["admin"], run["chat"]["id"], f"roll back {renamed}", reply_to=root)
    deliver(client)
    assert said_after(client, hub["owner"], run, before)[-1] == (
        f"Proposal {renamed} is rolled back: version 2 of the setting is a copy of the one before version 1."
    )
    assert client.get("/v1/branding").json()["name"] is None


# Who may call the tool


def test_builder_tool_answers_the_agent_of_a_builder_run_of_an_admin_alone(client, hub, hub_db):
    change = {"kind": "features", "summary": "Monitor off", "target": {"layer": "hub"}, "set": {"monitor": False}}
    refused = f"error: only the agent of a chat run of @builder in project {PROJECT} proposes a change"
    assert rpc(client, hub["owner"], call(3, TOOL, change))["content"][0]["text"] == refused, "no run"
    # the agent of another profile's run
    worker = owners_worker(client, hub)
    review = created(client, hub["owner"], "Review", agents=("reviewer",))
    sent(client, hub["owner"], review["id"], "@reviewer look at the diff")
    spec = claim(client, worker)
    assert spec["profile"]["handle"] == "reviewer", spec
    moved(client, worker, spec["id"], "running", session_id="sess-0")
    reviewer = {"Authorization": worker["headers"]["Authorization"], "X-Evo-Run": str(spec["id"])}
    assert proposed(client, reviewer, **change) == refused
    ended(client, worker, spec["id"])
    run = started(client, hub, worker=worker)
    branding = {**change, "kind": "branding", "target": {}}
    for arguments, said in (
        ({**change, "kind": "secrets"}, "bad arguments for builder__propose: kind: must be one of 'branding', "),
        ({**change, "summary": " "}, "summary: 1 to 500 characters; nothing proposed"),
        ({**change, "target": {"layer": "everywhere"}}, "bad arguments for builder__propose: target.layer: "),
        ({**change, "set": {}}, "the change sets and unsets no field; nothing proposed"),
        ({**change, "set": {"chats": "no"}}, "chats: true turns it on and false off, nothing else; nothing changed"),
        ({**change, "target": {"layer": "project"}}, "monitor: it is turned on or off for the whole hub only, "),
        (
            {**change, "kind": "agent", "target": {"agent": PROJECT}, "set": {"name": "x"}},
            f"@{PROJECT} is the default agent of project {PROJECT}, which @builder does not change",
        ),
        ({**branding, "set": {"logo": "x"}}, "logo: an image, which an admin uploads; @builder may unset it, never"),
        ({**branding, "set": {}, "unset": ["name"]}, "name: the layer does not set it, so there is nothing to unset; "),
    ):
        assert proposed(client, run["agent"], **arguments).startswith(f"error: {said}"), arguments
    b = tables.builder_proposals
    assert sql(hub_db, select(func.count()).select_from(b)) == [(0,)], "a refused change is never kept"
    # the dispatcher is no admin of the project any more
    demoted = client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=credentials_api.WRITER, headers=hub["admin"]
    )
    assert demoted.status_code == 200, demoted.text
    assert proposed(client, run["agent"], **change) == (
        f"error: @builder proposes for an admin of project {PROJECT}, and {OWNER} is none now"
    )
    assert sql(hub_db, select(func.count()).select_from(b)) == [(0,)]
    assert audit_rows(hub_db, "builder") == []


def test_builder_proposals_table_refuses_a_row_whose_state_and_columns_disagree(client, hub, hub_db):
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    user_id = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0][0]
    row = {
        "project_id": project_id,
        "kind": "agent",
        "target": {"agent": "reviewer"},
        "change": {"sets": {"name": "Reviewer two"}, "unset": []},
        "summary": "Rename @reviewer",
        "base_version": 1,
        "proposed_by": user_id,
    }
    decided = {"decided_by": user_id, "decided_at": func.now()}
    refused = {
        "an unknown kind": {"kind": "secrets"},
        "an empty summary": {"summary": ""},
        "a negative base version": {"base_version": -1},
        "pending yet decided": decided,
        "rejected by nobody": {"state": "rejected"},
        "applied without the version it made": {"state": "applied", **decided},
        "rolled back without the version of the rollback": {"state": "rolled_back", **decided, "applied_version": 2},
        "failed with a version": {"state": "failed", **decided, "applied_version": 2},
    }
    proposals = tables.builder_proposals
    for why, changes in refused.items():
        with pytest.raises(psycopg.errors.CheckViolation):
            sql(hub_db, insert(proposals).values({**row, **changes}))
            pytest.fail(f"the table took a row with {why}")
    rolled_back = {"state": "rolled_back", **decided, "applied_version": 2, "rollback_version": 3}
    policy_change = {"kind": "policy", "target": {}, "base_version": 0}
    for changes in ({}, {"state": "failed", **decided, "error": "it moved"}, rolled_back, policy_change):
        sql(hub_db, insert(proposals).values({**row, **changes}))
    states = select(proposals.c.kind, proposals.c.state).where(proposals.c.project_id == project_id)
    assert sql(hub_db, states.order_by(proposals.c.id)) == [
        ("agent", "pending"),
        ("agent", "failed"),
        ("agent", "rolled_back"),
        ("policy", "pending"),
    ]


# Proposals an intake made (plan problem-intake): no run, no chat; approving one writes the problem and its lines

INTAKE_LINE = {
    "source_kind": "messenger",
    "source_uri": "messenger://shop-group/1",
    "excerpt": "We ring the depot every morning.",
    "signal": "workaround",
    "label": "internal",
}
INTAKE_CANDIDATE = {
    "statement": "Shops cannot tell when a delivery comes",
    "who": "shop owners",
    "when": "before the delivery",
    "workaround": "they ring the depot",
    "basis": {
        "statement": {"quote": "we ring the depot"},
        "who": "inferred",
        "when": "inferred",
        "workaround": "inferred",
    },
    "evidence": [INTAKE_LINE, {**INTAKE_LINE, "source_kind": "email", "source_uri": "email://depot/42"}],
}


def intake_proposal(client, hub) -> int:
    """The writer someone-else starts intake I-1 and puts one new candidate problem; the id of its pending proposal."""
    base = f"/v1/projects/{PROJECT}/intakes"
    sources = [{"kind": "messenger", "label": "internal", "name": "Shop group"}]
    assert client.post(base, json={"sources": sources}, headers=hub["other"]).status_code == 201
    result = {"schema": "evo-intake/1", "candidates": [INTAKE_CANDIDATE], "promises": [], "questions": []}
    put = client.put(f"{base}/I-1", json=result, headers=hub["other"])
    assert put.status_code == 200, put.text
    return put.json()["outcome"]["proposals"][0]["proposal_id"]


def test_builder_proposals_of_an_intake_are_approved_like_any_other(client, hub):
    proposal_id = intake_proposal(client, hub)
    assert shown(client, hub["reader"], proposal_id)["state"] == "pending"
    assert answer(client, hub["owner"], proposal_id, "approve").json()["state"] == "applied"
    found = problem(client, hub["reader"], "P-1")
    assert (found["statement"], found["evidence_count"], found["source_count"]) == (INTAKE_CANDIDATE["statement"], 2, 2)


def test_builder_waiting_proposals_show_an_intake_s_to_a_writer_and_not_to_a_reader(client, hub):
    proposal_id = intake_proposal(client, hub)
    waiting = f"{PROPOSALS}/waiting"
    found = client.get(waiting, headers=hub["other"]).json()
    assert [(p["id"], p["kind"], p["state"], p["approver"], p["link"]) for p in found] == [
        (proposal_id, "problem", "pending", "project_writer", None)
    ]
    assert client.get(f"{waiting}?kind=problem", headers=hub["other"]).json() == found
    assert client.get(f"{waiting}?kind=opportunity", headers=hub["other"]).json() == []
    assert [p["id"] for p in client.get(waiting, headers=hub["owner"]).json()] == [proposal_id]
    assert client.get(waiting, headers=hub["reader"]).json() == []  # a reader decides no proposal
    assert client.get(f"{waiting}?kind=nothing", headers=hub["other"]).status_code == 422
    assert answer(client, hub["reader"], proposal_id, "approve").status_code == 403
    assert answer(client, hub["other"], proposal_id, "approve").json()["state"] == "applied"
    assert problem(client, hub["reader"], "P-1")["statement"] == INTAKE_CANDIDATE["statement"]
    assert client.get(waiting, headers=hub["other"]).json() == []


class MemberHub:
    """``evo_agents.hub.client.Hub`` over the in-process app, as the member whose headers it has."""

    def __init__(self, client, headers: dict):
        self.client, self.headers = client, headers

    def call(self, method: str, path: str, body=None):
        response = self.client.request(method, path, json=body, headers=self.headers)
        payload = response.json() if response.content else None
        if response.status_code >= 400:
            raise HubError(payload["message"], response.status_code, payload["error"])
        return payload


def intake_cli(monkeypatch, member: MemberHub, *argv: str) -> int:
    """``evo-agents hub intake ARGV --project PROJECT`` as ``member``; its exit status."""
    monkeypatch.setattr(cli_intake, "signed_in", lambda: (member, None))
    args = build_parser().parse_args(["hub", "intake", *argv, "--project", PROJECT])
    return args.func(args)


def test_builder_proposals_of_an_intake_are_listed_and_answered_with_the_cli(client, hub, monkeypatch, capsys):
    proposal_id = intake_proposal(client, hub)
    reader, writer = MemberHub(client, hub["reader"]), MemberHub(client, hub["other"])
    assert intake_cli(monkeypatch, reader, "proposal", "list") == 0
    assert "No proposal of problem or opportunity waits for your answer" in capsys.readouterr().out
    assert intake_cli(monkeypatch, reader, "proposal", "approve", str(proposal_id)) != 0
    assert "writer role" in capsys.readouterr().err
    assert intake_cli(monkeypatch, writer, "proposal", "list", "--kind", "problem") == 0
    listed = capsys.readouterr().out
    assert listed.splitlines()[1].split()[:5] == [str(proposal_id), "new", "problem", "P-1", OTHER]
    assert "Intake I-1: a new problem" in listed
    assert intake_cli(monkeypatch, writer, "proposal", "show", str(proposal_id)) == 0
    printed = capsys.readouterr().out
    assert f"Proposal {proposal_id} of project {PROJECT}: new problem P-1, pending" in printed
    assert f"statement: {INTAKE_CANDIDATE['statement']}" in printed
    assert f"evidence: [workaround] messenger messenger://shop-group/1: {INTAKE_LINE['excerpt']}" in printed
    assert intake_cli(monkeypatch, writer, "proposal", "approve", str(proposal_id)) == 0
    assert capsys.readouterr().out.strip() == f"Approved proposal {proposal_id}: new problem P-1 is made."
    found = problem(client, hub["reader"], "P-1")
    assert (found["statement"], found["evidence_count"]) == (INTAKE_CANDIDATE["statement"], 2)
    assert intake_cli(monkeypatch, writer, "proposal", "reject", str(proposal_id)) != 0  # applied already: 409
    assert "applied already" in capsys.readouterr().err


# Proposals of an opportunity (plan opportunity-intake, criteria a1 and a2)

OPPORTUNITY = {
    "statement": "Sell the shelf scanner to the depot",
    "beneficiary": "the depot's night shift",
    "leverage": "the depot asked for a demo twice",
}


def opportunity(client, headers, ref: str) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/opportunities/{ref}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_builder_proposal_of_an_opportunity_waits_for_a_writer_makes_it_writes_its_next_revision_and_rolls_back(
    client, hub
):
    run = started(client, hub, "@builder note the depot opportunity")
    for arguments, said in (
        ({"set": {**OPPORTUNITY, "budget_usd": 5}}, "budget_usd: an opportunity has no such field; nothing changed"),
        ({"set": {"statement": "x"}}, "beneficiary: Field required"),
        ({"set": {"rank": 2}, "unset": ["leverage"]}, "leverage: an opportunity always has one"),
        ({"target": {"opportunity": "O-4"}, "set": OPPORTUNITY}, f"project {PROJECT} has no opportunity O-4"),
        (
            {"target": {"opportunity": "o-1"}, "set": OPPORTUNITY},
            "bad arguments for builder__propose: target.opportunity",
        ),
        ({"set": {**OPPORTUNITY, "status": "missed"}}, "close_reason"),
        ({"set": {**OPPORTUNITY, "answers": ["P-7"]}}, "P-7"),
        ({"set": {**OPPORTUNITY, "owner": READER}}, READER),
        ({"target": {"problem": "P-1"}, "set": OPPORTUNITY}, "the target of a opportunity proposal names opportunity"),
    ):
        text = proposed(client, run["agent"], kind="opportunity", summary="s", **arguments)
        assert text.startswith("error:") and said in text, (arguments, text)
    new = made_proposal(client, run["agent"], kind="opportunity", summary="The depot", set=OPPORTUNITY)
    assert (shown(client, hub["reader"], new)["approver"], shown(client, hub["reader"], new)["base_version"]) == (
        "project_writer",
        0,
    )
    assert shown(client, hub["reader"], new)["target"] == {"opportunity": "O-1"}
    assert answer(client, hub["reader"], new, "approve").status_code == 403
    assert answer(client, hub["other"], new, "approve").json()["state"] == "applied"
    made = opportunity(client, hub["reader"], "O-1")
    assert (made["revision"], made["statement"], made["created_by"]) == (1, OPPORTUNITY["statement"], OTHER)
    changed = made_proposal(
        client,
        run["agent"],
        kind="opportunity",
        summary="Close it",
        target={"opportunity": "O-1"},
        set={"status": "captured", "close_reason": "the depot signed"},
    )
    assert answer(client, hub["owner"], changed, "approve").json()["applied_version"] == 2
    assert opportunity(client, hub["reader"], "O-1")["status"] == "captured"
    again = answer(client, hub["owner"], changed, "approve")
    assert again.status_code == 409
    assert rollback(client, hub["owner"], changed).json()["state"] == "rolled_back"
    back = opportunity(client, hub["reader"], "O-1")
    assert (back["revision"], back["status"], back["close_reason"]) == (3, "open", None)
    assert rollback(client, hub["owner"], new).status_code == 409  # it changed since: roll back by hand


def test_builder_proposal_of_an_opportunity_carries_evidence_made_with_it_or_alone_and_removed_on_rollback(client, hub):
    run = started(client, hub, "@builder note the depot opportunity")
    line = {
        "source_kind": "email",
        "source_uri": "email://depot/42",
        "excerpt": "Could you show the scanner to the night shift?",
        "signal": "wish",
        "label": "internal",
    }
    wrong = proposed(client, run["agent"], kind="problem", summary="s", set=PROBLEM, evidence=[line])
    assert wrong.startswith("error:") and "evidence" in wrong, wrong
    for bad in ({**line, "label": "personal"}, {**line, "excerpt": "x" * 301}, {**line, "signal": "rumour"}):
        text = proposed(client, run["agent"], kind="opportunity", summary="s", set=OPPORTUNITY, evidence=[bad])
        assert text.startswith("error:") and "evidence" in text, text
    twice = proposed(client, run["agent"], kind="opportunity", summary="s", set=OPPORTUNITY, evidence=[line, line])
    assert twice.startswith("error:"), twice
    first = made_proposal(client, run["agent"], kind="opportunity", summary="Depot", set=OPPORTUNITY, evidence=[line])
    assert "evidence" in shown(client, hub["reader"], first)["change"]
    assert answer(client, hub["other"], first, "approve").json()["state"] == "applied"
    url = f"/v1/projects/{PROJECT}/opportunities/O-1/evidence"
    listed = client.get(url, headers=hub["reader"]).json()
    assert [(e["code"], e["signal"], e["added_by"]) for e in listed] == [("E-1", "wish", OTHER)]
    found = opportunity(client, hub["reader"], "O-1")
    assert (found["evidence_count"], found["source_count"]) == (1, 1)
    second = made_proposal(
        client,
        run["agent"],
        kind="opportunity",
        summary="More",
        target={"opportunity": "O-1"},
        evidence=[{**line, "source_uri": "email://depot/43", "signal": "external_demand"}],
    )
    assert shown(client, hub["owner"], second)["base_version"] == 1
    assert answer(client, hub["owner"], second, "approve").json()["applied_version"] == 1
    assert opportunity(client, hub["reader"], "O-1")["revision"] == 1
    assert [e["code"] for e in client.get(url, headers=hub["reader"]).json()] == ["E-1", "E-2"]
    assert rollback(client, hub["owner"], second).json()["state"] == "rolled_back"
    assert [e["code"] for e in client.get(url, headers=hub["reader"]).json()] == ["E-1"]
    alone = proposed(client, run["agent"], kind="opportunity", summary="Alone", evidence=[line])
    assert alone.startswith("error:") and "target.opportunity" in alone, alone


# Proposals of an action (plan plugin-market, criterion a8): a new plugin, its install, its listing


NEW_PLUGIN = {
    "name": "northwind-stock",
    "industry": "retail",
    "need": "Count the stock of each\n shop every night",
    "owner": "northwind",
    "visibility": "private",
    "price": 49.5,
    "categories": ["inventory"],
}


def run_inbox(client, run: dict) -> list[str]:
    """The texts the hub put in the inbox of the run of @builder, taken and acknowledged."""
    path, headers = f"/v1/worker/runs/{run['spec']['id']}/inbox", run["worker"]["headers"]
    held = client.post(path, json={}, headers=headers).json()["messages"]
    if held:
        client.post(path, json={"ack": held[-1]["id"]}, headers=headers)
    return [item["text"] for item in held]


def test_builder_plugin_proposal_hands_the_run_its_task_once_an_admin_approves_and_installs_nothing(
    client, hub, hub_db
):
    run = started(client, hub, "@builder our shops need a stock count plugin")
    wrong = proposed(client, run["agent"], kind="plugin", summary="A stock plugin", set={**NEW_PLUGIN, "price": 0})
    assert "price: 0.01 to 100000 USD" in wrong and "nothing proposed" in wrong
    unknown = proposed(client, run["agent"], kind="plugin", summary="A stock plugin", set={"name": "Stock"})
    assert "industry: a plugin proposal needs it" in unknown and "name: a plugin's id" in unknown
    taken = proposed(client, run["agent"], kind="plugin", summary="Again", set={**NEW_PLUGIN, "name": "builder"})
    assert "this hub has a plugin builder already" in taken
    before = len(thread(client, hub["owner"], run["chat"]["id"], run["mention"]["id"]))
    proposal_id = made_proposal(
        client, run["agent"], kind="plugin", summary="Make a stock count plugin for the shops", set=NEW_PLUGIN
    )
    pending = shown(client, hub["reader"], proposal_id)
    assert (pending["state"], pending["kind"], pending["base_version"], pending["approver"]) == (
        "pending",
        "plugin",
        0,
        "project_admin",
    )
    assert pending["target"] == {} and pending["change"]["set"]["need"] == "Count the stock of each shop every night"
    assert (pending["opened_run_id"], pending["outcome"]) == (None, None)
    (shown_in_thread,) = said_after(client, hub["owner"], run, before)
    assert "It changes a new plugin northwind-stock for project" in shown_in_thread
    assert "in the repo northwind/northwind-stock. Approving it hands @builder the task" in shown_in_thread
    assert shown_in_thread.endswith("Approving does it once; it does not roll back.")
    assert notices(hub_db) == [(OWNER, proposal_id, "plugin")]
    assert run_inbox(client, run) == [], "nothing is handed before an approval"
    # a writer decides none
    assert "needs the admin role on project" in message(answer(client, hub["other"], proposal_id, "approve"), 403)
    # owner approves it in the thread: the hub hands the running session of @builder the task
    deliver(client)
    sent(client, hub["owner"], run["chat"]["id"], f"approve {proposal_id}", reply_to=run["mention"]["id"])
    deliver(client)
    done = shown(client, hub["reader"], proposal_id)
    assert (done["state"], done["applied_version"], done["decided_by"]) == ("applied", 1, OWNER)
    assert done["opened_run_id"] == run["spec"]["id"]
    assert done["outcome"] == f"@builder took the task to make the plugin in the thread, as run #{run['spec']['id']}"
    task, said = said_after(client, hub["owner"], run, before)[1:]
    assert task.startswith(f"@builder {OWNER} approved proposal {proposal_id}. Make the plugin northwind-stock")
    assert (
        "`gh repo create northwind/northwind-stock --template maycuatroi1/evo-plugin-template --private --clone`"
    ) in task
    assert "EVO_SELLER_PUBLISH=true" in task and "You set no secret yourself." in task
    assert "kind plugin_install (repo northwind/northwind-stock, tag v0.1.0)" in task
    assert said == f"Proposal {proposal_id} is approved by {OWNER}: {done['outcome']}."
    approval, handed = run_inbox(client, run)  # the session reads the thread: owner's reply, then the hub's task
    assert approval.endswith(f"approve {proposal_id}") and handed.startswith("the hub wrote in the thread")
    assert "gh repo create northwind/northwind-stock --template maycuatroi1/evo-plugin-template" in handed
    # the hub installed nothing, added no repo, and the action never rolls back
    plugins, repos = tables.plugins, tables.project_repos
    assert sql(hub_db, select(plugins.c.name).where(plugins.c.name == "northwind-stock")) == []
    assert sql(hub_db, select(repos.c.name).where(repos.c.name == "northwind-stock")) == []
    assert "is an action (plugin), done once, which never rolls back" in message(
        rollback(client, hub["owner"], proposal_id), 409
    )
    assert "is applied already" in message(answer(client, hub["owner"], proposal_id, "approve"), 409)
    # rejected, another hands nothing
    other = made_proposal(
        client, run["agent"], kind="plugin", summary="A second one", set={**NEW_PLUGIN, "name": "northwind-till"}
    )
    assert answer(client, hub["owner"], other, "reject").json()["state"] == "rejected"
    assert run_inbox(client, run) == []


def test_builder_plugin_proposal_stays_pending_when_its_approver_is_not_in_the_chat(client, hub, hub_db):
    run = started(client, hub, "@builder our shops need a stock count plugin")
    proposal_id = made_proposal(client, run["agent"], kind="plugin", summary="A stock plugin", set=NEW_PLUGIN)
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    hub_admin = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == ADMIN))[0][0]
    grants = tables.grants
    sql(
        hub_db,
        update(grants).where(grants.c.project_id == project_id, grants.c.user_id == hub_admin).values(role="admin"),
    )
    members = tables.chat_members
    gone = update(members).where(members.c.chat_id == run["chat"]["id"], members.c.user_id == hub_admin)
    sql(hub_db, gone.values(removed_at=func.now()))
    refused = message(answer(client, hub["admin"], proposal_id, "approve"), 409)
    assert "who is not in that chat" in refused
    assert shown(client, hub["reader"], proposal_id)["state"] == "pending"
    assert run_inbox(client, run) == []


def test_builder_plugin_install_proposal_is_for_a_hub_admin_and_installs_the_release_once(
    client, hub, hub_db, tmp_path
):
    from tests.hub.test_market import MarketGitHub
    from tests.plugins.market_samples import LUMEN_REPO, LUMEN_SIGNER, lumen_release
    from tests.plugins.samples import FakeSigstore

    github = MarketGitHub()
    github.releases[LUMEN_REPO] = [lumen_release(tmp_path / "v1", "1.0.0")]
    client.app.state.market_transport = github.transport()
    client.app.state.plugin_sigstore = FakeSigstore()
    run = started(client, hub, "@builder install the lumen notes plugin")
    wrong = proposed(client, run["agent"], kind="plugin_install", summary="Install lumen", set={"repo": "lumen"})
    assert "repo: OWNER/REPO on GitHub" in wrong
    proposal_id = made_proposal(
        client,
        run["agent"],
        kind="plugin_install",
        summary="Install lumen notes",
        set={"repo": LUMEN_REPO, "tag": "v1.0.0"},
    )
    missing = made_proposal(
        client, run["agent"], kind="plugin_install", summary="Install none", set={"repo": "lumen-labs/none"}
    )
    assert shown(client, hub["owner"], proposal_id)["approver"] == "hub_admin"
    assert notices(hub_db) == [(ADMIN, proposal_id, "plugin_install"), (ADMIN, missing, "plugin_install")]
    plugins = tables.plugins
    installed = select(plugins.c.name, plugins.c.installed_version).where(plugins.c.name == "lumen-notes")
    assert sql(hub_db, installed) == [], "nothing is installed before an approval"
    assert "needs a hub admin" in message(answer(client, hub["owner"], proposal_id, "approve"), 403)
    done = answer(client, hub["admin"], proposal_id, "approve")
    assert done.status_code == 200, done.text
    assert (done.json()["state"], done.json()["applied_version"], done.json()["opened_run_id"]) == ("applied", 1, None)
    assert done.json()["outcome"] == (
        "the hub installed plugin lumen-notes 1.0.0 and trusts its signer for its publisher from now on"
    )
    assert sql(hub_db, installed) == [("lumen-notes", "1.0.0")]
    p = tables.plugin_publishers
    assert sql(hub_db, select(p.c.publisher, p.c.signer)) == [("lumen", LUMEN_SIGNER)]
    assert "never rolls back" in message(rollback(client, hub["admin"], proposal_id), 409)
    failed = answer(client, hub["admin"], missing, "approve").json()
    assert failed["state"] == "failed" and "has no release" in failed["error"]
    assert run_inbox(client, run) == [], "an install hands @builder nothing"


def test_builder_plugin_install_of_a_trusted_owner_is_decided_by_the_admin_of_a_self_managed_project(
    client, hub, hub_db, tmp_path
):
    from tests.hub.test_market import MarketGitHub
    from tests.plugins.market_samples import LUMEN_REPO, lumen_release
    from tests.plugins.samples import FakeSigstore

    github = MarketGitHub()
    github.releases[LUMEN_REPO] = [lumen_release(tmp_path / "v1", "1.0.0")]
    client.app.state.market_transport = github.transport()
    client.app.state.plugin_sigstore = FakeSigstore()
    run = started(client, hub, "@builder install the lumen notes plugin")
    asked = {"repo": LUMEN_REPO, "tag": "v1.0.0"}
    before = made_proposal(client, run["agent"], kind="plugin_install", summary="Install lumen notes", set=asked)
    assert shown(client, hub["owner"], before)["approver"] == "hub_admin"
    # a hub admin turns the state on for another owner: the install of this repo stays a hub admin's
    body = {"enabled": True, "trusted_owners": ["northwind"]}
    url = f"/v1/admin/projects/{PROJECT}/self-managed"
    assert client.put(url, json=body, headers=hub["admin"]).status_code == 200
    other_owner = made_proposal(client, run["agent"], kind="plugin_install", summary="Install again", set=asked)
    assert shown(client, hub["owner"], other_owner)["approver"] == "hub_admin"
    assert "needs a hub admin" in message(answer(client, hub["owner"], other_owner, "approve"), 403)
    # lumen-labs trusted (written in capitals: owners are compared without case): the project's admin decides
    body = {"enabled": True, "trusted_owners": ["Lumen-Labs"]}
    assert client.put(url, json=body, headers=hub["admin"]).status_code == 200
    own = made_proposal(client, run["agent"], kind="plugin_install", summary="Install a third time", set=asked)
    assert shown(client, hub["owner"], own)["approver"] == "project_admin"
    assert shown(client, hub["owner"], before)["approver"] == "project_admin"
    assert notices(hub_db)[-1] == (OWNER, own, "plugin_install")
    assert "needs the admin role" in message(answer(client, hub["other"], own, "approve"), 403)
    done = answer(client, hub["owner"], own, "approve")
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "applied"
    assert done.json()["outcome"] == f"the hub installed plugin lumen-notes 1.0.0 for project {PROJECT} alone"
    plugins, publishers = tables.plugins, tables.plugin_publishers
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    held = select(plugins.c.installed_version, plugins.c.project_id).where(plugins.c.name == "lumen-notes")
    assert sql(hub_db, held) == [("1.0.0", project_id)]
    assert sql(hub_db, select(publishers.c.publisher)) == [], "the hub trusts no publisher for it"


def test_builder_plugin_listing_proposal_hands_the_run_a_pull_request_or_a_paid_listing(client, hub, hub_db):
    run = started(client, hub, "@builder list our stock plugin")
    listing = {"repo": "northwind/northwind-stock", "tag": "v0.1.0", "industries": ["retail"]}
    wrong = proposed(
        client, run["agent"], kind="plugin_listing", summary="List it", set={**listing, "price": "free", "seller": "nw"}
    )
    assert "seller: a free plugin is sold by no seller" in wrong
    free = made_proposal(
        client, run["agent"], kind="plugin_listing", summary="List it free", set={**listing, "price": "free"}
    )
    paid = made_proposal(
        client,
        run["agent"],
        kind="plugin_listing",
        summary="Sell it",
        set={**listing, "price": 49.5, "seller": "northwind", "categories": ["inventory"]},
    )
    assert shown(client, hub["owner"], free)["approver"] == "project_admin"
    assert run_inbox(client, run) == []
    for proposal_id in (free, paid):
        done = answer(client, hub["owner"], proposal_id, "approve")
        assert done.status_code == 200, done.text
        assert (done.json()["state"], done.json()["opened_run_id"]) == ("applied", run["spec"]["id"])
        assert done.json()["outcome"].startswith("@builder took the task to list the plugin in the thread")
    pull_request, sale = run_inbox(client, run)
    assert f"@builder {OWNER} approved proposal {free}. List the free plugin of northwind/northwind-stock" in (
        pull_request
    )
    assert "on the market maycuatroi1/evo-agents-market: fork it and open a pull request" in pull_request
    assert "`evo-agents plugin market-check market.json --releases`" in pull_request
    assert "for sale at 49.5 USD per hub per month for the seller northwind" in sale
    assert "`evo-agents hub market listing add PLUGIN --publisher PUBLISHER --signer SIGNER --usd 49.5`" in sale
    assert "`evo-agents hub market bundle upload PLUGIN FILE --signature FILE`" in sale
