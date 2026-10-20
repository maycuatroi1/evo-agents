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

from evo_agents.hub import plugin_calls, plugin_events, tables
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
