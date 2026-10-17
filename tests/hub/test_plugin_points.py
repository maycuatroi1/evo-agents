"""The extension points of the hub, as criteria a5 and a22 of the plan hub-agent-plugins name them: a plugin, core or
isolated, contributes a channel of notifications, routes under /v1/x/<plugin>/, MCP tools named after it on the hub's
/mcp, periodic jobs run through the job queue, and subscriptions to events (a run's state, a chat message, a plan
step) delivered at least once. Each is there only in a project that enabled the plugin, and the hub checks that on
every call; an MCP tool is listed only where the run's agent profile allows it, with the description of the installed
version.

The isolated plugin here is tests/fixtures/plugins/sample, whose backend runs for real in a plugin host reached over
its own HTTP app (``host``), or a stand-in host that answers what a test says (``FakeHost``). The core plugin is
``tests.hub.core_notes``, with a table of its own that a revision appended to the hub's chain of migrations creates
(``notes``); Telegram, the core plugin evo-ak ships, is ``tests.hub.test_telegram``'s.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import column, func, insert, select, table, update

from evo_agents.hub import migrate as hub_migrate
from evo_agents.hub import plugin_calls, plugin_events, plugin_jobs, runs, tables
from evo_agents.hub.plugin_calls import PluginCallError, PluginHostClient, PluginHostUnavailable
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.notifications import channel_class, notify
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import core_notes
from tests.hub.test_mcp import MCP_HEADERS, call, request
from tests.hub.test_runs import OTHER, OWNER, PLAN, PROJECT, READER, add_worker, claim, dispatched, members
from tests.plugins import backends

HOST_TOKEN = "plugin-host-shared-" + secrets.token_hex(16)
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
ASKED = ["runs:read", "chats:read", "plans:read", "notifications:send"]


def sample_manifest(**changes) -> dict:
    """The manifest of tests/fixtures/plugins/sample as the store holds it, asking for ``ASKED``."""
    data = json.loads((backends.FIXTURES / "sample" / "evo-plugin.json").read_text())
    return {**data, "permissions": ASKED, **changes}


def install(db, data: dict | None = None, *, tier: str = "isolated") -> int:
    """Plugin ``data`` (the sample by default) installed on the hub, as the store holds a published version."""
    data = data or sample_manifest()
    p, v, users = tables.plugins, tables.plugin_versions, tables.users
    with live.engine(db).begin() as conn:
        admin_id = conn.execute(select(users.c.id).where(users.c.login == live.ADMIN)).scalar_one()
        plugin_id = conn.execute(
            insert(p).values(name=data["id"], publisher=data["publisher"]).returning(p.c.id)
        ).scalar_one()
        row = {"plugin_id": plugin_id, "version": data["version"], "sha256": "c" * 64, "size": 10, "r2_key": "k"}
        row |= {"publisher": data["publisher"], "tier": tier, "manifest": data, "signature": {"kind": "ed25519"}}
        row |= {"signature_kind": "ed25519", "signed_by": "ed25519:0123", "published_by": admin_id}
        conn.execute(insert(v).values(row))
        installed = {"installed_version": data["version"], "installed_by": admin_id, "installed_at": func.now()}
        conn.execute(update(p).where(p.c.id == plugin_id).values(installed))
    return plugin_id


def enable(client, headers, name: str = "sample", permissions=ASKED) -> None:
    response = client.post(
        f"/v1/projects/{PROJECT}/plugins/{name}/enable", json={"permissions": list(permissions)}, headers=headers
    )
    assert response.status_code == 200, response.text


def disable(client, headers, name: str = "sample") -> None:
    response = client.post(f"/v1/projects/{PROJECT}/plugins/{name}/disable", headers=headers)
    assert response.status_code == 200, response.text


class FakeHost:
    """A stand-in for the plugin host: it records each call and answers what ``answers`` says for its method (a
    result, a function of the params, or an exception to raise)."""

    DEFAULTS = {
        "route": {"status": 200, "headers": {"content-type": "application/json"}, "body": "e30="},
        "tool": {"content": [{"type": "text", "text": "ok"}]},
        "job": {},
        "event": {},
        "channel": {"delivered": True},
    }

    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    async def call(self, plugin: str, method: str, params: dict, timeout: float = 30.0):
        self.calls.append(SimpleNamespace(plugin=plugin, method=method, params=params))
        answer = self.answers.get(method, self.DEFAULTS[method])
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    async def aclose(self) -> None:
        pass


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, public_url="https://hub.test", plugin_host_token=HOST_TOKEN)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """The members of ``tests.hub.test_runs`` (owner an admin of the project) and the sample plugin installed."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    install(hub_db)
    return headers


@pytest.fixture
def fake(client) -> FakeHost:
    host = FakeHost()
    client.app.state.plugin_host = host
    return host


@pytest.fixture
def host(client, tmp_path):
    """The sample plugin's backend in a real plugin host, which the api reaches over the host's HTTP app."""
    from evo_agents.hub.plugin_host_service import create_host_app
    from evo_agents.plugins.host import Host

    runner = Host(tmp_path / "plugins", allow_shared_user=True)
    runner.place(backends.manifest("sample"), backends.bundle("sample"), backends.init("sample"))
    hosted = PluginHostClient("http://plugin-host", HOST_TOKEN)
    hosted._client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_host_app(runner, HOST_TOKEN)),
        base_url="http://plugin-host",
        headers={"Authorization": f"Bearer {HOST_TOKEN}"},
    )
    client.app.state.plugin_host = hosted
    yield hosted
    client.portal.call(runner.stop_all)


def on_hub(client, work):
    """Run ``work(app)``, a coroutine function, in the loop of the hub ``client`` drives."""
    return client.portal.call(work, client.app)


def rpc(client, headers, message: dict, project: str | None = PROJECT):
    sent = {**headers, **MCP_HEADERS} | ({"X-Evo-Project": project} if project else {})
    response = client.post("/mcp", json=message, headers=sent)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def tool_names(client, headers, **options) -> list[str]:
    return [tool["name"] for tool in rpc(client, headers, request(1, "tools/list"), **options)["tools"]]


def deliveries(db) -> list[tuple]:
    d = tables.plugin_deliveries
    return live.sql(db, select(d.c.kind, d.c.contribution, d.c.state, d.c.attempts).order_by(d.c.id))


async def _notify_owner(app) -> int:
    u, projects = tables.users, tables.projects
    async with app.state.engine.begin() as conn:
        user_id = (await conn.execute(select(u.c.id).where(u.c.login == OWNER))).scalar_one()
        project_id = (await conn.execute(select(projects.c.id).where(projects.c.name == PROJECT))).scalar_one()
        return await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind="run_failed",
            title="Run 1 failed: the secret question of the run",
            project_id=project_id,
            link="/p/evo-agents/runs/1",
        )


def deliver(client):
    return on_hub(client, lambda app: plugin_events.deliver(app.state.engine, host=app.state.plugin_host))


JOBS = table("procrastinate_jobs", column("id"), column("task_name"))  # procrastinate's own table


def set_step(client, headers, step: int, status: str) -> None:
    """Set the status of step ``step`` of the plan, as `evo harness step` does through the hub."""
    revision = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=headers).json()["revision"]
    body = {"section": "steps", "step": step, "updates": {"status": status}, "if_revision": revision}
    patched = client.patch(f"/v1/projects/{PROJECT}/plans/{PLAN}", json=body, headers=headers)
    assert patched.status_code == 200, patched.text


def end_run(client, worker, run_id: int) -> None:
    ended = client.post(
        f"/v1/worker/runs/{run_id}/state", json={"state": "failed", "error": "x"}, headers=worker["headers"]
    )
    assert ended.status_code == 200, ended.text


# Every point through the backend of an isolated plugin


def test_plugin_route_mcp_job_event_and_channel_reach_the_backend_of_an_isolated_plugin(client, hub, hub_db, host):
    enable(client, hub["owner"])

    answered = client.get(f"/v1/x/sample/{PROJECT}/hello", headers=hub["reader"])
    assert answered.status_code == 200, answered.text
    assert answered.json() == {"greeting": "hello", "project": PROJECT}  # the backend got the caller's project
    assert answered.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in answered.headers["content-security-policy"]

    assert "sample__echo" in tool_names(client, hub["other"])
    echoed = rpc(client, hub["other"], call(2, "sample__echo", {"text": "hi there"}))
    assert echoed == {"content": [{"type": "text", "text": "hi there"}], "isError": False}

    engine = client.app.state.engine
    ran = on_hub(client, lambda app: plugin_jobs.run_job(engine, None, "sample", "tick", host=host))
    assert ran["projects"] == [PROJECT] and ran["result"] == {}

    worker = add_worker(client, hub["owner"], "mac-mini")
    dispatched(client, hub["owner"], [2])
    assert claim(client, worker) is not None  # queued to leased: one run.state event
    on_hub(client, _notify_owner)  # a notice of the project: one notification for the channel
    assert deliver(client)["delivered"] == 2
    assert deliveries(hub_db) == [("run.state", "run-changed", "delivered", 1), ("notification", "log", "delivered", 1)]


# Routes


def test_plugin_route_answers_404_where_the_plugin_is_not_enabled_and_passes_the_caller(client, hub, fake):
    hello = f"/v1/x/sample/{PROJECT}/hello"
    assert "not enabled in project evo-agents" in client.get(hello, headers=hub["owner"]).json()["message"]
    assert client.get("/v1/x/nowhere/evo-agents/hello", headers=hub["owner"]).status_code == 404
    enable(client, hub["owner"])
    assert client.get(hello, headers=hub["stranger"]).status_code == 404  # no grant: as for an unknown project
    assert client.get(hello, headers=hub["admin"]).status_code == 404  # a hub admin without a grant
    assert client.get(hello).status_code == 401
    assert client.get(f"/v1/x/sample/{PROJECT}/nope", headers=hub["owner"]).status_code == 404
    assert client.post(hello, headers=hub["owner"]).status_code == 405  # GET only, as it declares no methods

    sent = client.get(
        f"{hello}/a/b?x=1",
        headers={**hub["reader"], "Cookie": "evo_session=s", "Accept": "application/json", "X-Other": "y"},
    )
    assert sent.status_code == 200, sent.text
    (made,) = fake.calls
    assert (made.plugin, made.method, made.params["route"]) == ("sample", "route", "hello")
    assert made.params["caller"] == {"login": READER, "project": PROJECT, "role": "reader"}
    request_sent = made.params["request"]
    assert (request_sent["method"], request_sent["path"], request_sent["query"]) == ("GET", "/a/b", {"x": "1"})
    assert set(request_sent["headers"]) <= {"accept", "user-agent"}  # never the credential or the cookie
    assert "authorization" not in json.dumps(made.params).lower() and "evo_session" not in json.dumps(made.params)

    disable(client, hub["owner"])
    assert client.get(hello, headers=hub["owner"]).status_code == 404


def test_plugin_route_answer_is_served_with_safe_headers_only(client, hub, fake):
    enable(client, hub["owner"])
    hello = f"/v1/x/sample/{PROJECT}/hello"
    page = {
        "status": 201,
        "headers": {"Content-Type": "text/html", "Set-Cookie": "evo_session=stolen", "ETag": '"v1"', "Location": "x"},
        "body": "PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
    }
    fake.answers["route"] = page
    served = client.get(hello, headers=hub["owner"])
    assert served.status_code == 201
    assert served.headers["content-type"] == "application/octet-stream"  # a page would run on the hub's origin
    assert served.headers["content-disposition"] == "attachment"
    assert "set-cookie" not in served.headers and "location" not in served.headers
    assert served.headers["etag"] == '"v1"' and served.headers["cache-control"] == "no-store"
    fake.answers["route"] = {"status": 200, "headers": {"content-type": "text/plain; charset=utf-7"}, "body": "aGk="}
    assert client.get(hello, headers=hub["owner"]).headers["content-type"] == "text/plain; charset=utf-8"

    fake.answers["route"] = {"status": 200, "body": "not base64!"}
    assert "not base64" in client.get(hello, headers=hub["owner"]).json()["message"]
    fake.answers["route"] = {"headers": {}}  # no status: plugin/1 does not allow it
    refused = client.get(hello, headers=hub["owner"])
    assert refused.status_code == 502 and "plugin/1 does not allow" in refused.json()["message"]
    fake.answers["route"] = PluginCallError(-32000, "the backend's own failure")
    assert client.get(hello, headers=hub["owner"]).status_code == 502
    fake.answers["route"] = PluginHostUnavailable("the backend is in backoff")
    unavailable = client.get(hello, headers=hub["owner"])
    assert unavailable.status_code == 503 and "backoff" in unavailable.json()["message"]


# MCP tools


def test_plugin_mcp_tool_is_listed_with_the_installed_description_only_where_enabled(client, hub, fake):
    assert not [name for name in tool_names(client, hub["owner"]) if "__" in name]
    assert "does not enable plugin sample" in rpc(client, hub["owner"], call(2, "sample__echo"))["content"][0]["text"]
    enable(client, hub["owner"])
    listed = rpc(client, hub["owner"], request(1, "tools/list"))["tools"]
    assert len(listed) == 23 and listed[-1]["name"] == "sample__echo"
    assert listed[-1]["description"] == "Echo"  # the manifest's title, as no description is given: never the backend's
    assert listed[-1]["inputSchema"] == {"type": "object"}
    assert tool_names(client, hub["owner"], project=None)[-1] != "sample__echo"  # no project, no plugin tools
    assert "__" not in "".join(tool_names(client, hub["stranger"]))

    fake.answers["tool"] = {"content": [{"type": "text", "text": "pong"}], "is_error": True}
    result = rpc(client, hub["reader"], call(3, "sample__echo", {"text": "x"}))
    assert result == {"content": [{"type": "text", "text": "pong"}], "isError": True}
    assert fake.calls[-1].params == {
        "tool": "echo",
        "arguments": {"text": "x"},
        "caller": {"login": READER, "project": PROJECT, "role": "reader"},
    }
    fake.answers["tool"] = {"content": "not a list"}
    assert "plugin/1 does not allow" in rpc(client, hub["owner"], call(4, "sample__echo"))["content"][0]["text"]


def test_plugin_mcp_tool_shows_to_a_run_agent_only_when_its_profile_tool_axis_allows(client, hub, hub_db, fake):
    install(hub_db, sample_manifest(id="other-plugin"))
    enable(client, hub["owner"])
    enable(client, hub["owner"], "other-plugin")
    allowlist = {"mode": "allowlist", "allow": ["read", "mcp:evo-hub", "mcp:sample"]}
    for handle, tools in (("open", {"mode": "all"}), ("narrow", allowlist)):
        made = client.post(
            f"/v1/projects/{PROJECT}/agents",
            json={"handle": handle, "name": handle, "permissions": {"tool": tools}},
            headers=hub["owner"],
        )
        assert made.status_code == 201, made.text
    worker = add_worker(client, hub["owner"], "mac-mini")
    beat = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 1, "runs": []}
    beat["features"] = [runs.AGENT_PROFILES]  # its daemon applies profiles, so it claims a run whose tools are cut
    assert client.post("/v1/worker/heartbeat", json=beat, headers=worker["headers"]).status_code == 200
    agent = {"Authorization": worker["headers"]["Authorization"]}
    seen = {}
    for handle in ("open", "narrow"):
        (run,) = dispatched(client, hub["owner"], [2], profile=handle)
        assert claim(client, worker)["id"] == run["id"]
        seen[handle] = tool_names(client, {**agent, "X-Evo-Run": str(run["id"])}, project=None)
        if handle == "narrow":
            text = rpc(client, {**agent, "X-Evo-Run": str(run["id"])}, call(2, "other-plugin__echo"), project=None)
            assert "agent profile leaves it out" in text["content"][0]["text"]
        end_run(client, worker, run["id"])
    assert {"sample__echo", "other-plugin__echo"} <= set(seen["open"])
    assert "sample__echo" in seen["narrow"] and "other-plugin__echo" not in seen["narrow"]


# Jobs


def test_plugin_job_is_queued_by_its_cron_through_the_job_queue_once_at_a_time(client, hub, hub_db, fake):
    install(hub_db, sample_manifest(id="nightly", contributes={"jobs": [{"name": "sweep", "cron": "30 2 * * *"}]}))
    hourly = datetime(2026, 10, 17, 5, 0, tzinfo=UTC)
    engine = client.app.state.engine
    assert on_hub(client, lambda app: plugin_jobs.due(engine, hourly)) == []  # no project enables either
    enable(client, hub["owner"])
    enable(client, hub["owner"], "nightly")
    assert on_hub(client, lambda app: plugin_jobs.due(engine, hourly)) == [("sample", "tick")]  # 0 * * * *
    at_night = datetime(2026, 10, 17, 2, 30, tzinfo=UTC)
    assert on_hub(client, lambda app: plugin_jobs.due(engine, at_night)) == [("nightly", "sweep")]

    async def fire(app):
        manager = app.state.jobs._manager  # the api's manager over its own pool, as the worker's would be
        first = await plugin_jobs.fire(app.state.engine, manager, int(hourly.timestamp()))
        again = await plugin_jobs.fire(app.state.engine, manager, int(hourly.timestamp()))
        return first, again

    first, again = on_hub(client, fire)
    assert first == {"deferred": ["sample:tick"]} and again == {"deferred": []}  # one waits already
    queued = select(func.count()).select_from(JOBS).where(JOBS.c.task_name == "hub.plugin_job")
    assert live.sql(hub_db, queued) == [(1,)]

    ran = on_hub(client, lambda app: plugin_jobs.run_job(app.state.engine, None, "sample", "tick", host=fake))
    assert ran["projects"] == [PROJECT]
    assert [(c.method, c.params) for c in fake.calls] == [("job", {"job": "tick", "projects": [PROJECT]})]
    disable(client, hub["owner"])
    skipped = on_hub(client, lambda app: plugin_jobs.run_job(app.state.engine, None, "sample", "tick", host=fake))
    assert skipped == {"skipped": "no project enables the plugin"} and len(fake.calls) == 1


# Events and channels


def test_plugin_event_of_each_kind_is_stored_for_the_plugins_that_take_it_and_may_read_it(client, hub, hub_db, fake):
    reads_runs = sample_manifest(
        id="runs-only",
        permissions=["runs:read"],
        contributes={"events": [{"name": "runs", "on": ["run.state"]}, {"name": "steps", "on": ["plan.step"]}]},
    )
    install(hub_db, reads_runs)
    enable(client, hub["owner"])
    enable(client, hub["owner"], "runs-only", ["runs:read"])
    worker = add_worker(client, hub["owner"], "mac-mini")
    dispatched(client, hub["owner"], [2])
    run = claim(client, worker)
    set_step(client, hub["owner"], 3, "blocked")
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "helper", "name": "Helper"}, headers=hub["owner"]
    )
    assert made.status_code == 201, made.text
    chat = client.post("/v1/chats", json={"title": "Talk", "people": [], "agents": ["helper"]}, headers=hub["owner"])
    assert chat.status_code == 201, chat.text
    message = client.post(f"/v1/chats/{chat.json()['id']}/messages", json={"body": "hello"}, headers=hub["owner"])
    assert message.status_code == 201, message.text

    d, p = tables.plugin_deliveries, tables.plugins
    query = select(p.c.name, d.c.kind, d.c.contribution, d.c.payload).join(p, p.c.id == d.c.plugin_id)
    stored = live.sql(hub_db, query.where(d.c.kind != "notification").order_by(d.c.id))
    assert [row[:3] for row in stored] == [
        ("runs-only", "run.state", "runs"),
        ("sample", "run.state", "run-changed"),
        ("sample", "plan.step", "run-changed"),  # runs-only takes steps but may not read plans: none for it
        ("sample", "chat.message", "run-changed"),
    ]
    assert stored[0][3] == {
        "run_id": run["id"],
        "run_kind": "step",
        "from": "queued",
        "to": "leased",
        "actor": "worker",
        "plan_id": PLAN,
        "step": "2",
    }
    assert {key: stored[2][3][key] for key in ("plan_id", "step", "from", "to")} == {
        "plan_id": PLAN,
        "step": "3",
        "from": "pending",
        "to": "blocked",
    }
    said = stored[3][3]
    assert said["chat_id"] == chat.json()["id"] and said["by"] == "person" and "hello" not in json.dumps(said)

    report = deliver(client)
    assert report["delivered"] >= 4 and report["failed"] == 0
    sent = [(c.plugin, c.params["event"], c.params["kind"]) for c in fake.calls if c.method == "event"]
    assert sent == [
        ("runs-only", "runs", "run.state"),
        ("sample", "run-changed", "run.state"),
        ("sample", "run-changed", "plan.step"),
        ("sample", "run-changed", "chat.message"),
    ]
    assert all(isinstance(c.params["delivery"], int) and c.params["project"] == PROJECT for c in fake.calls)


def test_plugin_event_is_delivered_at_least_once_retried_and_checked_again_at_delivery(client, hub, hub_db, fake):
    enable(client, hub["owner"])
    worker = add_worker(client, hub["owner"], "mac-mini")
    (run,) = dispatched(client, hub["owner"], [2])
    claim(client, worker)
    fake.answers["event"] = PluginHostUnavailable("the backend is restarting")
    assert deliver(client)["retried"] == 1
    assert deliveries(hub_db) == [("run.state", "run-changed", "pending", 1)]
    assert deliver(client)["retried"] == 0  # not due again before its backoff
    live.sql(hub_db, update(tables.plugin_deliveries).values(next_at=func.now()))
    fake.answers.pop("event")
    assert deliver(client)["delivered"] == 1
    first, second = fake.calls
    assert first.params["delivery"] == second.params["delivery"]  # the same delivery, tried again: at least once

    # Checked again at delivery: a project that no longer enables the plugin gets nothing from it.
    end_run(client, worker, run["id"])
    disable(client, hub["owner"])
    assert deliver(client)["failed"] >= 1
    d = tables.plugin_deliveries
    errors = {row[0] for row in live.sql(hub_db, select(d.c.last_error).where(d.c.state == "failed"))}
    assert errors == {"project evo-agents does not enable plugin sample any more"}
    assert len(fake.calls) == 2


def test_plugin_channel_of_an_isolated_plugin_gets_the_project_notifications_without_their_text(
    client, hub, hub_db, fake
):
    on_hub(client, _notify_owner)
    assert deliveries(hub_db) == []  # not enabled: nothing for it
    enable(client, hub["owner"], permissions=ASKED)
    on_hub(client, _notify_owner)
    fake.answers["channel"] = {"delivered": True}
    assert deliver(client)["delivered"] == 1
    (made,) = fake.calls
    assert (made.method, made.params["channel"], made.params["project"]) == ("channel", "log", PROJECT)
    notification = made.params["notification"]
    assert (notification["login"], notification["kind"], notification["notice_kind"]) == (OWNER, "notice", "run_failed")
    assert (notification["text"], notification["link"]) == ("Run failed", "/p/evo-agents/runs/1")
    assert "secret question" not in json.dumps(made.params)  # the title stays on the hub

    on_hub(client, _notify_owner)
    fake.answers["channel"] = {"delivered": False}
    assert deliver(client)["failed"] == 1


def test_plugin_channel_of_a_core_plugin_is_a_kind_of_members_channel_telegram_s_among_them():
    from evo_agents.core_plugins.telegram.server import TelegramChannel

    assert channel_class("telegram") is TelegramChannel
    assert channel_class("nowhere") is None


# The core plugin notes: its own table through the hub's migrations, its routes, tool and events per project


@pytest.fixture
def installed(monkeypatch):
    """Core plugins installed as entry points: notes, and ghost, whose table no migration creates."""
    points = [
        EntryPoint("ghost", "tests.hub.core_notes:MISSING_TABLE", ENTRY_POINT_GROUP),
        EntryPoint("notes", "tests.hub.core_notes:MANIFEST", ENTRY_POINT_GROUP),
    ]
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def migrations(monkeypatch):
    """The hub's chain of migrations with the revision of notes appended after its head."""
    hub_config = hub_migrate.alembic_config

    def with_notes():
        config = hub_config()
        hub_versions = hub_migrate.script_location() / "versions"
        config.set_main_option("path_separator", "os")
        config.set_main_option("version_locations", os.pathsep.join((str(hub_versions), str(core_notes.MIGRATIONS))))
        return config

    monkeypatch.setattr(hub_migrate, "alembic_config", with_notes)


@pytest.fixture
def notes(installed, migrations, hub_db, tmp_path, github):
    core_notes.STEPS.clear()
    config = live.hub_config(hub_db, tmp_path, github, public_url="https://hub.test")
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
        yield SimpleNamespace(client=client, **headers)


def test_core_plugin_migration_puts_its_prefixed_table_in_the_hub_chain_and_the_store_records_it(notes, hub_db):
    assert live.sql(hub_db, select(hub_migrate.ALEMBIC_VERSION.c.version_num)) == [("9001",)]
    assert live.sql(hub_db, select(func.to_regclass("public.notes_entries"))) == [("notes_entries",)]
    store = notes.client.get("/v1/plugins/notes", headers=notes.admin)
    assert store.status_code == 200, store.text
    shown = store.json()
    assert (shown["state"], shown["installed_version"], shown["installed_by"]) == ("installed", "1.0.0", None)
    (version,) = shown["versions"]
    assert (version["tier"], version["signature_kind"], version["published_by"]) == ("core", "image", None)
    # ghost owns a table no migration made: the hub does not record it; Telegram, with nothing per project, neither
    names = [plugin["name"] for plugin in notes.client.get("/v1/plugins", headers=notes.admin).json()]
    assert names == ["notes"]
    for action in ("install", "upgrade", "remove"):
        refused = notes.client.post(f"/v1/plugins/notes/{action}", json={}, headers=notes.admin)
        assert refused.status_code == 409 and "core plugin of the hub's image" in refused.json()["message"]


def test_core_plugin_migration_rule_tables_carry_the_plugin_prefix_and_come_from_the_hub_chain(hub_db):
    from evo_agents.core_plugins.telegram import MANIFEST as TELEGRAM
    from evo_agents.plugins.manifest import ManifestError, read_manifest

    telegram = read_manifest(TELEGRAM)
    assert telegram.tables() == ("telegram_links",) and not telegram.per_project()
    assert "telegram_links" in tables.metadata.tables  # a table of the hub's own chain (0015)
    stray = {**core_notes.MANIFEST, "contributes": {"tables": [{"name": "entries"}]}}
    with pytest.raises(ManifestError, match="must start with notes_"):
        read_manifest(stray)
    isolated = {**core_notes.MANIFEST, "tier": "isolated", "contributes": {"tables": [{"name": "notes_x"}]}}
    with pytest.raises(ManifestError, match="only a core plugin owns tables"):
        read_manifest(isolated)


def test_core_plugin_disabled_answers_404_keeps_its_data_and_enabled_again_finds_it(notes, hub_db):
    client = notes.client
    entries = f"/v1/x/notes/{PROJECT}/entries"
    message = client.get(entries, headers=notes.owner).json()["message"]
    assert "plugin notes is not enabled in project evo-agents" in message
    assert "notes__count" not in tool_names(client, notes.owner)
    refused = rpc(client, notes.owner, call(2, "notes__count"))
    assert refused["isError"] and "does not enable plugin notes" in refused["content"][0]["text"]

    enable(client, notes.owner, "notes", ["plans:read"])
    added = client.post(entries, json={"body": "first note"}, headers=notes.other)
    assert added.status_code == 201, added.text
    assert client.get(entries, headers=notes.reader).json() == [{"body": "first note", "author": OTHER}]
    assert rpc(client, notes.reader, call(3, "notes__count"))["content"][0]["text"] == "1 notes in evo-agents"
    set_step(client, notes.owner, 1, "in_progress")
    assert on_hub(client, lambda app: plugin_events.deliver(app.state.engine))["delivered"] == 1
    (step,) = core_notes.STEPS
    assert (step["event"], step["kind"], step["payload"]["to"]) == ("steps", "plan.step", "in_progress")

    disable(client, notes.owner, "notes")
    assert client.get(entries, headers=notes.owner).status_code == 404
    assert client.post(entries, json={"body": "lost"}, headers=notes.owner).status_code == 404
    assert "notes__count" not in tool_names(client, notes.owner)
    kept = live.sql(hub_db, select(core_notes.entries.c.body))
    assert kept == [("first note",)]  # turning it off deleted nothing

    enable(client, notes.owner, "notes", ["plans:read"])
    assert client.get(entries, headers=notes.owner).json() == [{"body": "first note", "author": OTHER}]


def test_core_plugin_page_is_native_and_framed_nowhere(notes):
    from dataclasses import replace

    from evo_agents.hub.server.plugin_pages import ticket

    client = notes.client
    listed = client.get(f"/v1/projects/{PROJECT}/plugins", headers=notes.reader)
    assert listed.status_code == 200, listed.text
    (plugin,) = listed.json()
    assert (plugin["name"], plugin["tier"], plugin["title"], plugin["enabled"]) == ("notes", "core", "Notes", False)
    page = {"name": "entries", "title": "Notes", "description": "", "icon": "notebook-pen", "menu": True}
    assert plugin["pages"] == [{**page, "native": True, "url": None}]

    enable(client, notes.owner, "notes", ["plans:read"])
    shown = client.get(f"/v1/projects/{PROJECT}/plugins/notes", headers=notes.reader).json()
    assert shown["enabled"] and shown["pages"] == [{**page, "native": True, "url": None}]
    # the web carries a core plugin's page itself: the hub frames none, a valid ticket included
    secret = live.session_secret()
    client.app.state.config = replace(client.app.state.config, session_secret=secret)
    framed = f"/v1/plugin-ui/notes/{PROJECT}/{ticket(secret, 'notes', PROJECT)}/index.html"
    assert client.get(framed).status_code == 404


def test_core_plugin_mcp_failure_is_the_call_s_never_the_hub_s(notes, monkeypatch):
    client = notes.client
    enable(client, notes.owner, "notes", ["plans:read"])

    async def broken(params, context):
        raise RuntimeError("the core plugin's own bug")

    found = plugin_calls.load
    monkeypatch.setattr(plugin_calls, "load", lambda ref: broken if ref.endswith(":count") else found(ref))
    failed = rpc(client, notes.owner, call(2, "notes__count"))
    assert failed["isError"] and "plugin notes failed: RuntimeError" in failed["content"][0]["text"]
    assert "own bug" not in failed["content"][0]["text"]  # its message stays in the log
    assert client.get(f"/v1/x/notes/{PROJECT}/entries", headers=notes.owner).status_code == 200


def test_plugin_route_and_other_points_have_versions_and_a_backend_error_reaches_the_caller():
    from evo_agents.plugins import compat

    assert set(compat.POINT_VERSIONS) >= {"channels", "routes", "mcp_tools", "jobs", "events", "tables"}
    newer = {"routes": [{"name": "hello", "api": 2}]}
    assert "version 2 of that extension point" in compat.problem({"evo-agents": ">=0.16"}, 1, contributes=newer)
    assert compat.problem({"evo-agents": ">=0.16"}, 1, contributes={"routes": [{"name": "hello", "api": 1}]}) is None
    assert asyncio.run(_host_error_is_raised()) == "the backend's error (code -32000)"


async def _host_error_is_raised() -> str:
    """The client to the plugin host raises the error a backend answered, which the host returns with status 200."""

    def answered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": -32000, "message": "the backend's error"}})

    hosted = PluginHostClient("http://plugin-host", HOST_TOKEN)
    hosted._client = httpx.AsyncClient(transport=httpx.MockTransport(answered), base_url="http://plugin-host")
    try:
        await hosted.call("sample", "tool", {})
    except PluginCallError as exc:
        return f"{exc} (code {exc.code})"
    finally:
        await hosted.aclose()
    return "no error"
