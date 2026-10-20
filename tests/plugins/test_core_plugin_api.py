"""The API of core plugins (``evo_agents.hub.server.core_plugin_api``), as criteria a2 and a3 of the plan
external-core-plugins name them, through the sample core plugin northwind-chat (``tests/fixtures/core_plugins``),
which lives outside the package evo_agents, loads through an entry point and reaches the hub through the API alone:
a member links a chat from a web session, gets a decision with its buttons, answers it with a button or in words,
answers a proposal, talks to an agent in a direct chat and reads its answer in the chat; and the API refuses what the
web routes refuse, a session that ended, a wrong secret, a project kept from the channel's clearance, a project that
grants no chats:write, another plugin's config, a class of channel that does not load, a webhook under another
plugin's prefix, and a plugin written for another version of the API.

Northwind Chat itself is ``tests/plugins/fake_northwind.py``. What the plugin may import is
``tests/plugins/test_core_plugin_imports.py``'s; the surface and its versions ``test_core_plugin_api_surface.py``'s.
"""

from __future__ import annotations

import importlib
import logging
import secrets
from functools import partial
from importlib.metadata import EntryPoint
from importlib.metadata import entry_points as installed
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.hub import plugin_calls, plugin_events, tables
from evo_agents.hub.openapi import document
from evo_agents.hub.server import core_plugin_api as hub
from evo_agents.hub.server import notifications, plugin_routes
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_store import insert_message
from evo_agents.hub.server.sealing import KEY_BYTES
from evo_agents.hub.server.security import CSRF_HEADER, SESSION_COOKIE, Principal, csrf_token, hash_token
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP, core_plugins
from tests.hub.conftest import github, hub_db, hub_template, pg_server  # noqa: F401 (fixtures)
from tests.hub.live import sql
from tests.hub.test_curator import night  # noqa: F401 (a fixture, on this module's web_client)
from tests.hub.test_decisions import OPTIONS, QUESTION, asked
from tests.hub.test_plan_runs import started
from tests.hub.test_plans import LEVELS
from tests.hub.test_review_runs import (
    held_review,
    proposed,
    review,  # noqa: F401 (a fixture)
)
from tests.hub.test_runs import OTHER, OWNER, PROJECT, audit_rows, report
from tests.plugins.fake_northwind import API_URL, GROUP, SECRET, TOKEN, USER, FakeNorthwind, button, text

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "core_plugins"
PLUGIN, KIND = "northwind-chat", "northwind"
NORTHWIND = EntryPoint(PLUGIN, "northwind_chat:manifest", ENTRY_POINT_GROUP)
LUMEN = EntryPoint("lumen-relay", "lumen_relay:manifest", ENTRY_POINT_GROUP)
BROKEN = EntryPoint("lumen-broken", "lumen_relay:BROKEN", ENTRY_POINT_GROUP)
STRAY = EntryPoint("lumen-stray", "lumen_relay:STRAY", ENTRY_POINT_GROUP)
HOOK = f"/v1/hooks/{PLUGIN}/webhook"
LINK = "/v1/northwind-chat/link"
ORIGIN = "https://hub.test"
CHATS = ["chats:read", "chats:write"]


def installing(monkeypatch, *points: EntryPoint) -> None:
    """The core plugins of this process: ``points`` alone, read again."""
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def fake(monkeypatch) -> FakeNorthwind:
    """Northwind Chat in place of the real one, the sample plugins importable and installed: northwind-chat, and
    lumen-relay beside it."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    server = importlib.import_module("northwind_chat.server")
    service = FakeNorthwind()
    monkeypatch.setattr(server, "TRANSPORT", service.transport())
    installing(monkeypatch, LUMEN, NORTHWIND)
    yield service
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def web_client(hub_db, tmp_path, github, fake):
    """A hub with the sample plugins, a key for secrets and the web's sign-in, which ``night`` builds on."""
    changes = {**live.web_changes(github), "secrets_key": secrets.token_bytes(KEY_BYTES)}
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github, **changes)), base_url=ORIGIN) as client:
        yield client


def set_up(client, headers, db) -> None:
    """northwind-chat configured by a hub admin and enabled by the project with chats:read and chats:write; the
    project's hub sink clears internal for any location, which the channel's default clearance holds."""
    values = {"api_url": API_URL, "token": TOKEN, "webhook_secret": SECRET}
    written = client.post(f"/v1/plugins/{PLUGIN}/config", json={"values": values}, headers=headers["admin"])
    assert written.status_code == 200, written.text
    enable(client, headers, CHATS)
    sinks = tables.project_sinks
    sql(db, update(sinks).values(clearance={"level": "internal", "location": "any"}).where(sinks.c.kind == "hub"))


def enable(client, headers, permissions) -> None:
    path = f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/enable"
    response = client.post(path, json={"permissions": list(permissions)}, headers=headers["owner"])
    assert response.status_code == 200, response.text


def web(client, db, login: str) -> SimpleNamespace:
    """A new web session of ``login``: its id, and the headers of a read and of a write from a page of the hub."""
    token, session_id = live.web_session(db, login)
    cookie = {"Cookie": f"{SESSION_COOKIE}={token}"}
    write = {
        **cookie,
        CSRF_HEADER: csrf_token(client.app.state.config.session_secret, hash_token(token)),
        "Origin": ORIGIN,
    }
    return SimpleNamespace(id=session_id, read=cookie, write=write)


def hook(client, update: dict, secret: str = SECRET):
    return client.post(f"{HOOK}/{secret}", json=update)


def said(client, update: dict) -> str:
    response = hook(client, update)
    assert response.status_code == 200, response.text
    return response.json()["outcome"]


def linked(client, fake: FakeNorthwind, session: SimpleNamespace, group: str = GROUP, user: str = USER) -> None:
    assert said(client, text("/link", group_id=group, user_id=user)) == "link"
    url = fake.last(group, user)["text"].split()[1]
    query = dict(part.split("=", 1) for part in url.partition("?")[2].split("&"))
    shown = client.get(LINK, params=query, headers=session.read)
    assert shown.status_code == 200 and shown.json()["login"] == OWNER, shown.text
    made = client.post(LINK, json=query, headers=session.write)
    assert made.status_code == 201, made.text


def deliver(client) -> dict:
    engine, config = client.app.state.engine, client.app.state.config
    return client.portal.call(partial(notifications.deliver_notifications, engine, config=config))


def deliver_events(client) -> dict:
    engine, config = client.app.state.engine, client.app.state.config
    return client.portal.call(partial(plugin_events.deliver, engine, config=config))


def on_hub(client, work):
    async def run(engine):
        async with engine.begin() as conn:
            return await work(conn)

    return client.portal.call(partial(run, client.app.state.engine))


def channel_rows(db) -> list[tuple]:
    c = tables.notification_channels
    return sql(db, select(c.c.kind, c.c.config, c.c.token_id).order_by(c.c.id))


@pytest.fixture
def northwind(night, hub_db, fake) -> SimpleNamespace:
    """night's hub with northwind-chat set up, the owner's chat linked from a web session of theirs."""
    client, headers = night.client, night.headers
    set_up(client, headers, hub_db)
    session = web(client, hub_db, OWNER)
    linked(client, fake, session)
    return SimpleNamespace(**vars(night), session=session, fake=fake, db=hub_db)


# Linking


def test_a_member_links_a_chat_from_a_web_session_with_csrf_and_origin(night, hub_db, fake):
    client, headers = night.client, night.headers
    set_up(client, headers, hub_db)
    session = web(client, hub_db, OWNER)
    assert said(client, text("/link")) == "link"
    url = fake.last()["text"].split()[1]
    assert url.startswith(f"{ORIGIN}{LINK}?group_id={GROUP}&user_id={USER}&sig=")
    query = dict(part.split("=", 1) for part in url.partition("?")[2].split("&"))
    # A machine token, a write without the hub's Origin, or a forged link links nothing.
    assert client.post(LINK, json=query, headers=headers["owner"]).status_code == 403
    no_origin = {key: value for key, value in session.write.items() if key != "Origin"}
    refused = client.post(LINK, json=query, headers=no_origin)
    assert refused.status_code == 403 and "Origin" in refused.json()["message"]
    elsewhere = client.post(LINK, json=query, headers={**session.write, "Origin": "https://lumen.example"})
    assert elsewhere.status_code == 403
    assert client.post(LINK, json={**query, "user_id": "u-999"}, headers=session.write).status_code == 404
    assert channel_rows(hub_db) == []
    made = client.post(LINK, json=query, headers=session.write)
    assert made.status_code == 201 and made.json() == {"linked": True, "login": OWNER}
    assert channel_rows(hub_db) == [(KIND, {"group_id": GROUP, "user_id": USER}, session.id)]
    assert fake.last()["text"] == f"Linked to the hub as {OWNER}."
    assert audit_rows(hub_db, KIND) == [(f"{KIND}.link", f"{KIND} user:{OWNER}", OWNER, None)]
    # Another member linking the same chat takes it: a chat speaks for one member.
    theirs = web(client, hub_db, OTHER)
    assert client.post(LINK, json=query, headers=theirs.write).status_code == 201
    assert channel_rows(hub_db) == [(KIND, {"group_id": GROUP, "user_id": USER}, theirs.id)]


def test_a_chat_unlinks_with_stop_or_on_the_web_and_lives_no_longer_than_its_session(northwind):
    client, db, fake = northwind.client, northwind.db, northwind.fake
    assert said(client, text("/agents")) == "agents"
    assert fake.last()["text"] == f"@{PROJECT} ({PROJECT})"  # the project's default agent
    assert client.post("/v1/auth/logout", headers=northwind.session.write).status_code == 204
    assert said(client, text("/agents")) == "not_linked"  # find_member takes a live session alone
    assert said(client, text("/stop")) == "unlinked"  # and live=False finds it to unlink
    assert channel_rows(db) == []
    assert audit_rows(db, KIND)[-1] == (f"{KIND}.unlink", f"{KIND} user:{OWNER} by=bot", OWNER, None)
    session = web(client, db, OWNER)
    linked(client, fake, session)
    assert client.delete(LINK, headers=northwind.headers["owner"]).status_code == 403  # WebSession: no machine token
    gone = client.delete(LINK, headers=session.write)
    assert gone.status_code == 200 and gone.json() == {"unlinked": True}
    assert audit_rows(db, KIND)[-1] == (f"{KIND}.unlink", f"{KIND} user:{OWNER} by=web", OWNER, None)


def test_a_member_links_with_a_one_time_code_and_a_hub_admin_unlinks_any_members_chat(night, hub_db, fake):
    client, headers = night.client, night.headers
    set_up(client, headers, hub_db)
    session = web(client, hub_db, OWNER)
    assert client.post("/v1/northwind-chat/code", headers=headers["owner"]).status_code == 403  # a web session only
    made = client.post("/v1/northwind-chat/code", headers=session.write)
    assert made.status_code == 201, made.text
    code = made.json()["code"]
    assert said(client, text(f"/start {'x' * 32}")) == "link_failed"
    assert said(client, text(f"/start {code}")) == "linked"
    assert fake.last()["text"] == f"Linked to the hub as {OWNER}."
    assert channel_rows(hub_db) == [(KIND, {"group_id": GROUP, "user_id": USER}, session.id)]
    assert said(client, text(f"/start {code}", user_id="u-300")) == "link_failed"  # once
    codes = tables.channel_link_codes
    assert sql(hub_db, select(codes.c.kind, codes.c.used_at.is_not(None))) == [(KIND, True)]
    # A code whose session ended links nothing.
    later = client.post("/v1/northwind-chat/code", headers=session.write).json()["code"]
    assert client.post("/v1/auth/logout", headers=session.write).status_code == 204
    assert said(client, text(f"/start {later}", user_id="u-300")) == "link_failed"
    # A hub admin unlinks it, with a machine token as the hub's admin routes take; a member may not.
    u = tables.users
    ((user_id,),) = sql(hub_db, select(u.c.id).where(u.c.login == OWNER))
    path = f"/v1/northwind-chat/admin/users/{user_id}"
    assert client.delete(path, headers=headers["owner"]).status_code == 403
    gone = client.delete(path, headers=headers["admin"])
    assert gone.status_code == 200 and gone.json() == {"unlinked": True}
    assert audit_rows(hub_db, KIND)[-1] == (f"{KIND}.unlink", f"{KIND} user:{OWNER} by=admin", live.ADMIN, None)


def test_the_webhook_is_public_under_its_plugins_prefix_and_a_wrong_secret_is_404(northwind):
    client = northwind.client
    assert hook(client, text("/agents"), secret="not-the-secret").status_code == 404
    assert client.post(f"{HOOK}/{SECRET}", content=b"not json").json() == {"outcome": "ignored"}
    assert said(client, text("hello")) == "help"


# Decisions


def sent_decision(northwind) -> SimpleNamespace:
    client = northwind.client
    worker, run = started(client, northwind.headers)
    decision = asked(client, worker, run["id"])
    assert deliver(client) == {"delivered": 2, "retried": 0, "failed": 0}
    return SimpleNamespace(worker=worker, run=run, decision=decision, message=northwind.fake.last())


def test_a_decision_goes_to_the_chat_with_its_buttons_and_a_button_answers_it(northwind):
    client, db = northwind.client, northwind.db
    sent = sent_decision(northwind)
    decision_id = sent.decision["id"]
    assert sent.message["text"] == f"Decision #{decision_id}: {QUESTION}"
    assert sent.message["buttons"] == [
        {"text": option["label"], "data": f"d:{decision_id}:{option['key']}"} for option in OPTIONS
    ]
    deliveries = tables.notification_deliveries
    stored = sql(db, select(deliveries.c.external_id).where(deliveries.c.channel_id.is_not(None)))
    assert stored == [(sent.message["id"],)]
    assert said(client, button(f"d:{decision_id}:postgres")) == "answered"
    assert northwind.fake.last()["text"] == f"Answered by {OWNER}: Move to Postgres"
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=northwind.headers["owner"]).json()
    assert (shown["state"], shown["answer_option"], shown["answered_by"]) == ("answered", "postgres", OWNER)
    (row,) = audit_rows(db, "decision")
    assert row[1].endswith(f"option=postgres via={KIND}") and row[2] == OWNER
    # The web route's refusals come back in the route's words.
    assert said(client, button(f"d:{decision_id}:sqlite")) == "refused"
    assert f"decision {decision_id} is answered, not open" in northwind.fake.last()["text"]


def test_a_reply_to_a_decisions_message_answers_it_in_words(northwind):
    client = northwind.client
    sent = sent_decision(northwind)
    words = "Postgres, and keep the SQLite file a week."
    assert said(client, text(words, reply_to=sent.message["id"])) == "replied"
    assert (
        northwind.fake.last()["text"]
        == f"Your answer to decision #{sent.decision['id']} went to run #{sent.run['id']}."
    )
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{sent.decision['id']}", headers=northwind.headers["owner"])
    assert (shown.json()["state"], shown.json()["answer_text"]) == ("answered", words)
    assert said(client, text("to nothing", reply_to="msg-424242")) == "replied"
    assert northwind.fake.last()["text"] == "Only a decision takes an answer in words."


def restrict(db, level: str) -> None:
    sinks = tables.project_sinks
    sql(db, update(sinks).values(clearance={"level": level, "location": "any"}).where(sinks.c.kind == "hub"))


def test_a_project_kept_from_the_channels_clearance_gets_the_link_alone_and_takes_no_answer(northwind):
    client = northwind.client
    restrict(northwind.db, LEVELS[LEVELS.index("internal") + 1])  # above the channel's clearance, internal
    sent = sent_decision(northwind)
    decision_id = sent.decision["id"]
    assert sent.message["buttons"] == [] and QUESTION not in sent.message["text"]
    assert sent.message["text"] == f"Something of {PROJECT} waits for you: {ORIGIN}/inbox?decision={decision_id}"
    assert said(client, button(f"d:{decision_id}:postgres")) == "refused"
    assert "keeps its decisions and proposals off this channel" in northwind.fake.last()["text"]
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=northwind.headers["owner"])
    assert shown.json()["state"] == "open"


def test_a_chat_whose_session_ended_answers_nothing_and_is_turned_off(northwind):
    client, db = northwind.client, northwind.db
    sent = sent_decision(northwind)
    assert client.post("/v1/auth/logout", headers=northwind.session.write).status_code == 204
    assert said(client, button(f"d:{sent.decision['id']}:postgres")) == "not_linked"
    u = tables.users
    ((user_id,),) = sql(db, select(u.c.id).where(u.c.login == OWNER))

    async def notify(conn):
        return await notifications.notify(
            conn, user_id=user_id, kind="notice", notice_kind="run_failed", title="Failed"
        )

    on_hub(client, notify)
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}
    assert channel_rows(db)[0][1]["disabled_reason"] == hub.SESSION_ENDED


# Proposals


def test_a_proposal_takes_accept_from_the_chat_while_the_curator_is_on(review, hub_db, fake):
    client, headers = review.client, review.headers
    set_up(client, headers, hub_db)
    linked(client, fake, web(client, hub_db, OWNER))
    run_id = held_review(review)
    made = proposed(client, review.worker, run_id, kind="feature", title="A wait helper instead of sleep and tail")
    for state in ("verifying", "done"):
        assert report(client, review.worker, run_id, state).status_code == 200
    assert deliver(client)["failed"] == 0
    pid = made["id"]
    message = fake.last()
    assert message["text"] == f"Proposal #{pid}: A wait helper instead of sleep and tail"
    assert [item["data"] for item in message["buttons"]] == [f"p:{pid}:{a}" for a in ("accept", "reject", "defer")]
    off = {"values": {"curator": False}}
    switched = client.post(f"/v1/admin/projects/{PROJECT}/features", json=off, headers=headers["admin"])
    assert switched.status_code == 200, switched.text
    assert said(client, button(f"p:{pid}:accept")) == "refused"
    assert "curator is turned off for project" in fake.last()["text"]
    on = {"values": {"curator": True}}
    assert client.post(f"/v1/admin/projects/{PROJECT}/features", json=on, headers=headers["admin"]).status_code == 200
    assert said(client, button(f"p:{pid}:accept")) == "answered"
    assert fake.last()["text"] == f"Proposal #{pid} accepted by {OWNER}"
    a = tables.audit
    targets = sql(hub_db, select(a.c.target).where(a.c.action == "curator.proposal"))
    assert targets == [(f"{PROJECT} proposal:{pid} run:{run_id} answer=accept via={KIND}",)]


# Direct chats


def agent_said(client, db, chat_id: int, handle: str, body: str) -> int:
    """A message of agent ``handle`` in chat ``chat_id``, as its chat run would post it; its id."""
    ap, u = tables.agent_profiles, tables.users
    ((profile_id,),) = sql(db, select(ap.c.id).where(ap.c.handle == handle))
    ((user_id,),) = sql(db, select(u.c.id).where(u.c.login == OWNER))

    async def work(conn):
        reader = Principal(user_id=user_id, login=OWNER, admin=False, token_id=0, kind="machine", token_hash="")
        chat = await chat_access(conn, reader, chat_id)
        message_id, _ = await insert_message(conn, chat, body, thread_root_id=None, author_profile_id=profile_id)
        return message_id

    return on_hub(client, work)


def test_a_member_talks_to_an_agent_in_a_direct_chat_and_reads_its_answer_in_the_chat(northwind):
    client, db, fake = northwind.client, northwind.db, northwind.fake
    assert said(client, text(f"@{PROJECT} what is left of the plan?")) == "sent"
    c, cm = tables.chats, tables.chat_messages
    ((chat_id, kind),) = sql(db, select(c.c.id, c.c.kind))
    assert kind == "direct"
    ((message_id, body),) = sql(db, select(cm.c.id, cm.c.body).where(cm.c.author_user_id.is_not(None)))
    assert body == "what is left of the plan?"  # sent as the member; the hub answers that no worker takes it here
    assert audit_rows(db, "chat")[-1][0:3] == ("chat.message", f"chat:{chat_id} message:{message_id}", OWNER)
    answer = agent_said(client, db, chat_id, PROJECT, "Steps 2 and 3.")
    assert deliver_events(client)["delivered"] >= 1
    assert fake.last()["text"] == f"@{PROJECT}: Steps 2 and 3."
    # Delivered again, at least once as events are, it is not sent twice.
    deliveries = tables.plugin_deliveries
    sql(db, update(deliveries).values(state="pending", delivered_at=None, next_at=func.now()))
    sent = len(fake.messages)
    deliver_events(client)
    assert len(fake.messages) == sent
    channels = tables.notification_channels
    ((state,),) = sql(db, select(channels.c.config["state"]))
    assert state == {"sent": [answer]}

    # The plugin's source of the graph reads the chat as the hub labels it, while the project grants chats:read.
    async def history(conn):
        mine = hub.plugin("northwind_chat.server")
        return (
            await hub.chat_history_items(conn, mine, PROJECT),
            await hub.granted(conn, mine, PROJECT, "chats:write"),
            await hub.granted(conn, hub.plugin("lumen_relay"), PROJECT, "chats:read"),
        )

    items, writes, lumen_reads = on_hub(client, history)
    assert (writes, lumen_reads) == (True, False)
    ids = {item["id"] for item in items["items"]}
    assert {f"plugin.{PLUGIN}:message:{message_id}", f"plugin.{PLUGIN}:message:{answer}"} <= ids
    assert items["cursor"]["item"].startswith(f"plugin.{PLUGIN}:message:")


def test_a_project_that_does_not_grant_chats_write_takes_no_message(northwind):
    client, db = northwind.client, northwind.db
    pp = tables.project_plugins  # a grant made before the plugin asked for chats:write
    sql(db, update(pp).values(permissions=["chats:read"]))
    assert said(client, text(f"@{PROJECT} hello")) == "refused"
    assert f"does not grant plugin {PLUGIN} chats:write" in northwind.fake.last()["text"]
    assert sql(db, select(func.count()).select_from(tables.chat_messages)) == [(0,)]


# What one plugin cannot reach


def test_a_plugin_reads_its_own_config_alone_with_its_secrets_unsealed(northwind):
    client = northwind.client
    config = client.app.state.config
    written = client.post(
        "/v1/plugins/lumen-relay/config", json={"values": {"api_key": "lumen-key"}}, headers=northwind.headers["admin"]
    )
    assert written.status_code == 200, written.text

    async def read(conn):
        mine = await hub.plugin_config(conn, hub.plugin("northwind_chat.server"), config)
        lumen = await hub.plugin_config(conn, hub.plugin("lumen_relay"), config)
        forged = hub.Plugin("lumen-relay", ())
        with pytest.raises(ValueError, match="comes from plugin"):
            await hub.plugin_config(conn, forged, config)
        return mine, lumen

    mine, lumen = on_hub(client, read)
    assert (mine.values["token"], mine.values["webhook_secret"], mine.missing_secrets) == (TOKEN, SECRET, ())
    assert "api_key" not in mine.values and lumen.values == {"api_key": "lumen-key"}
    with pytest.raises(hub.NotACorePlugin):
        hub.plugin("evo_agents.hub.server.app")


def test_a_member_handle_session_or_plugin_the_api_did_not_make_is_refused(northwind):
    client = northwind.client

    async def forge(conn):
        member = hub.Member(1, OWNER, PLUGIN, KIND, 1, {})
        with pytest.raises(ValueError, match="Member comes from"):
            await hub.answer_decision(conn, member, 1, hub.Clearance("internal"), option="x")
        session = hub.Session(1, OWNER, 1)
        with pytest.raises(ValueError, match="Session comes from"):
            await hub.link_channel(conn, hub.plugin("northwind_chat"), session, KIND, {"group_id": "g"})
        with pytest.raises(ValueError, match="not a kind of channel"):
            await hub.find_member(conn, hub.plugin("northwind_chat"), "lumen", {"group_id": "g"})

    on_hub(client, forge)


def test_a_channel_class_that_does_not_load_fails_its_own_deliveries_alone(northwind, monkeypatch):
    client, db = northwind.client, northwind.db
    installing(monkeypatch, LUMEN, NORTHWIND, BROKEN)
    u, c = tables.users, tables.notification_channels
    ((user_id,),) = sql(db, select(u.c.id).where(u.c.login == OWNER))
    sql(db, insert(c).values(user_id=user_id, kind="lumen-broken", config={}))

    async def notify(conn):
        return await notifications.notify(
            conn, user_id=user_id, kind="notice", notice_kind="run_failed", title="Run failed"
        )

    on_hub(client, notify)
    assert deliver(client) == {"delivered": 2, "retried": 0, "failed": 1}  # the web and northwind go out
    d = tables.notification_deliveries
    ((error,),) = sql(db, select(d.c.last_error).where(d.c.state == "failed"))
    assert error.startswith("the class of channels of kind lumen-broken (plugin lumen-broken) did not load")
    assert notifications.channel_class("lumen-broken") is None


def test_a_plugin_reads_the_variables_named_for_it_and_a_transaction_of_its_context(monkeypatch, northwind):
    monkeypatch.setenv("EVO_HUB_NORTHWIND_CHAT_REGION", "eu")
    monkeypatch.setenv("EVO_HUB_LUMEN_RELAY_KEY", "lumen's")
    mine = hub.plugin("northwind_chat")
    assert dict(hub.plugin_environment(mine)) == {"EVO_HUB_NORTHWIND_CHAT_REGION": "eu"}
    context = hub.Context(PLUGIN, northwind.client.app.state.engine)

    async def users():
        async with hub.transaction(context) as conn:
            return (await conn.execute(select(func.count()).select_from(tables.users))).scalar_one()

    assert northwind.client.portal.call(users) >= 1
    with pytest.raises(ValueError, match="no engine"):
        hub.transaction(hub.Context(PLUGIN, None))


# Versions, routes and the OpenAPI document


def test_a_plugin_written_for_another_api_version_is_left_out_with_the_reason(monkeypatch, caplog):
    monkeypatch.syspath_prepend(str(FIXTURES))
    sample = importlib.import_module("northwind_chat")
    installing(monkeypatch, NORTHWIND)
    assert [plugin.manifest.id for plugin in core_plugins()] == [PLUGIN]
    monkeypatch.setattr(sample, "API", (1, 1))
    with caplog.at_level(logging.WARNING, logger=core.__name__):
        assert core_plugins() == []
    (record,) = [record for record in caplog.records if record.getMessage() == "core plugin not loaded"]
    assert record.entry_point == PLUGIN
    assert record.error == "UnsupportedApi: northwind_chat needs core plugin API 1.1; this hub serves 1.0"
    plugin_calls.core_registry.cache_clear()


def test_a_router_with_a_webhook_under_another_plugins_prefix_is_left_out(monkeypatch, caplog):
    monkeypatch.syspath_prepend(str(FIXTURES))
    installing(monkeypatch, NORTHWIND, STRAY)
    app = FastAPI()
    with caplog.at_level(logging.ERROR, logger=plugin_routes.__name__):
        plugin_routes.include(app)
    paths = set(plugin_routes.route_paths(app.router))
    assert f"{HOOK}/{{secret}}" in paths and LINK in paths
    assert "/v1/hooks/northwind-chat/steal" not in paths
    (record,) = [record for record in caplog.records if record.getMessage() == "core plugin route not included"]
    assert record.contribution == "lumen-stray:hooks" and record.paths == ["/v1/hooks/northwind-chat/steal"]
    plugin_calls.core_registry.cache_clear()


def test_the_openapi_document_is_the_same_with_the_sample_plugin_installed(monkeypatch):
    monkeypatch.syspath_prepend(str(FIXTURES))
    image = list(installed(group=ENTRY_POINT_GROUP))
    installing(monkeypatch, *image)
    before = document()
    installing(monkeypatch, *image, NORTHWIND, LUMEN)
    after = document()
    assert PLUGIN in {plugin.manifest.id for plugin in plugin_calls.core_registry().plugins}  # what it was made with
    assert after == before
    plugin_calls.core_registry.cache_clear()
