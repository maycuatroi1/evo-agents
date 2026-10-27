"""Sinks of kind channel (criterion a1 of the plan chat-bridges): a project's knowledge.yaml declares, per kind of
channel of a plugin (``telegram`` say), the clearance of what of its chats may go out to a group of that channel; the
hub stores it at register (``tests.hub.test_projects``) and ``chat_access.channel_allows`` reads it. Without such a
sink for a kind, that channel gets nothing of the project's chats: it fails closed. The rule that a 1:1 private channel
gets minimal messages (``telegram.restricted``) reads the hub sink alone, as before.

The first tests need no Postgres; the last ones register projects (northwind and lumen, fake names) on the hub."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="the hub extras are not installed")

from evo_agents.hub.server.chat_access import LabelProject, channel_allows, with_override
from tests.hub import pg

LEVELS = ["public", "internal", "customer", "secret"]
LOCATIONS = ["any", "domestic-only"]
INTERNAL = {"level": "internal", "location": "any", "integrity": "U"}


def project(name: str = "northwind", channels=None, clearance=None, default=INTERNAL) -> LabelProject:
    return LabelProject(
        id=1,
        name=name,
        levels=LEVELS,
        locations=LOCATIONS,
        default_label=dict(default),
        clearance=clearance or {"level": "internal", "location": "any"},
        channels=channels or {},
    )


def part(level: str, name: str = "northwind") -> dict:
    return {"level": level, "location": "any", "integrity": "U", "projects": [name]}


def test_channel_sink_lets_out_a_label_its_clearance_clears_and_nothing_above_it():
    northwind = project(channels={"telegram": {"level": "public"}})
    projects = {"northwind": northwind}
    assert channel_allows([part("public")], "telegram", projects)
    assert not channel_allows([part("internal")], "telegram", projects)
    assert not channel_allows([part("customer")], "telegram", projects)


def test_no_channel_sink_for_a_kind_lets_nothing_out_of_the_projects_chats():
    projects = {"northwind": project(channels={"telegram": {"level": "secret"}})}
    assert channel_allows([part("internal")], "telegram", projects)
    assert not channel_allows([part("public")], "matrix", projects), "no sink for the kind: fails closed"
    assert not channel_allows([part("public")], "telegram", {"northwind": project()}), "no channel sink at all"


def test_channel_sink_fails_closed_on_an_empty_unread_or_unknown_label():
    projects = {"northwind": project(channels={"telegram": {"level": "secret"}})}
    assert not channel_allows([], "telegram", projects)
    assert not channel_allows(None, "telegram", projects)
    assert not channel_allows("internal", "telegram", projects)
    assert not channel_allows([part("public", "lumen")], "telegram", projects), "a project the hub does not know"
    assert not channel_allows([{"level": "public", "projects": []}], "telegram", projects)
    customer = {"northwind": project(channels={"telegram": {"level": "customer"}})}
    assert not channel_allows([part("unheard-of")], "telegram", customer), "a level the ladder lacks reads as secret"
    unknown = {"northwind": project(channels={"telegram": {"level": "unheard-of"}})}
    assert not channel_allows([part("public")], "telegram", unknown), "a clearance the ladder lacks"


def test_channel_sink_of_every_project_a_label_names_must_clear_its_part():
    northwind = project(channels={"telegram": {"level": "internal"}})
    lumen = project("lumen", channels={"telegram": {"level": "public"}})
    both = {"northwind": northwind, "lumen": lumen}
    assert channel_allows([part("internal"), part("public", "lumen")], "telegram", both)
    assert not channel_allows([part("internal"), part("internal", "lumen")], "telegram", both)
    assert not channel_allows([part("internal"), part("public", "lumen")], "telegram", {"northwind": northwind})


def test_channel_sink_reads_the_chat_level_and_leaves_the_restricted_rule_to_the_hub_sink():
    customer = {"level": "customer", "location": "any"}
    northwind = project(channels={"telegram": {"level": "public"}}, clearance=customer)
    assert northwind.restricted, "a hub sink cleared for customer: minimal messages, as before"
    assert project(channels={"telegram": {"level": "secret"}}).restricted is False
    assert project(channels={"telegram": {"level": "public"}}, clearance=customer).restricted is True
    (lowered,) = with_override([northwind], {"level": "public", "projects": ["northwind"]})
    assert lowered.label["level"] == "public" and lowered.lowered
    assert lowered.restricted, "a lower chat level does not change the restricted rule"
    assert channel_allows([lowered.label], "telegram", {"northwind": lowered})
    assert not channel_allows([northwind.label], "telegram", {"northwind": northwind})
    (other,) = with_override([northwind], {"level": "public", "projects": ["lumen"]})
    assert other.label["level"] == "internal", "the level lowers only the projects it names"


# On the hub

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.chat_access import channel_cleared
    from tests.hub.live import sql
    from tests.hub.test_chats import OPEN_SINKS, agent, client, created, hub, on_hub, page, sent  # noqa: F401 (fixtures)
    from tests.hub.test_plans import registration
    from tests.hub.test_runs import PROJECT

TELEGRAM = {"id": "telegram", "kind": "channel", "clearance": {"level": "public", "location": "any"}}
on_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


@on_pg
def test_channel_sink_of_a_registered_project_lets_out_only_the_messages_of_a_chat_set_to_its_level(
    client, hub, hub_db
):
    registered = client.put(f"/v1/projects/{PROJECT}", json=registration([*OPEN_SINKS, TELEGRAM]), headers=hub["admin"])
    assert registered.status_code == 200, registered.text
    chat = created(client, hub["owner"], title="Bridged", agents=["reviewer"])
    before = sent(client, hub["owner"], chat["id"], "internal still")
    lowered = client.put(f"/v1/chats/{chat['id']}/label", json={"level": "public"}, headers=hub["owner"])
    assert lowered.status_code == 200, lowered.text
    after = sent(client, hub["owner"], chat["id"], "public now")
    m = tables.chat_messages
    labels = dict(sql(hub_db, select(m.c.id, m.c.label).where(m.c.chat_id == chat["id"])))

    async def cleared(conn):
        return [
            await channel_cleared(conn, labels[message["id"]], kind)
            for message in (before, after)
            for kind in ("telegram", "matrix")
        ]

    assert on_hub(client, cleared) == [False, False, True, False]
    assert lowered.json()["restricted"] is False


@on_pg
def test_channel_sink_absent_from_a_project_lets_none_of_its_chats_out(client, hub, hub_db):
    chat = created(client, hub["owner"], title="Not bridged", agents=["reviewer"])
    lowered = client.put(f"/v1/chats/{chat['id']}/label", json={"level": "public"}, headers=hub["owner"])
    assert lowered.status_code == 200, lowered.text
    said = sent(client, hub["owner"], chat["id"], "public, but no channel sink")
    m = tables.chat_messages
    (label,) = sql(hub_db, select(m.c.label).where(m.c.id == said["id"]))[0]

    async def cleared(conn):
        return await channel_cleared(conn, label, "telegram")

    assert on_hub(client, cleared) is False


# Bridges (criterion a4): the sample core plugin northwind-chat bridges a group of Northwind Chat, which is no
# Telegram, to a group chat of the hub, through the API alone.

if pg.DSN:
    import base64
    from dataclasses import replace
    from types import SimpleNamespace

    from sqlalchemy import insert, update

    from evo_agents.hub import chat_bridge_events
    from evo_agents.hub.blobs import BlobStore
    from evo_agents.hub.server import core_plugin_api as api
    from evo_agents.hub.server.chat_store import status_reaction
    from tests.hub.test_curator import night  # noqa: F401 (a fixture, on this module's web_client)
    from tests.hub.test_runs import OTHER, OWNER, audit_rows
    from tests.plugins import test_core_plugin_api as core
    from tests.plugins.fake_northwind import text
    from tests.plugins.test_core_plugin_api import fake, northwind, web_client  # noqa: F401 (fixtures)

ROOM, EVERYONE, KIND = "room-7", "*", "northwind"
SINK = {"level": "internal", "location": "any"}


def bridge_code(client, chat_id: int, headers, kind: str = KIND):
    return client.post(f"/v1/chats/{chat_id}/bridges/code", json={"kind": kind}, headers=headers)


def bridge_rows(db) -> list[tuple]:
    b = tables.chat_bridges
    return sql(db, select(b.c.kind, b.c.external_id, b.c.unlinked_via).order_by(b.c.id))


def channel_sink(db, clearance=SINK) -> None:
    """The project's sink of kind channel ``northwind``: what of its chats may go out to a group of Northwind Chat."""
    sinks, p = tables.project_sinks, tables.projects
    ((project_id,),) = sql(db, select(p.c.id).where(p.c.name == PROJECT))
    found = sql(db, select(sinks.c.sink_id).where(sinks.c.project_id == project_id, sinks.c.sink_id == KIND))
    if found:
        sql(db, update(sinks).values(clearance=clearance).where(sinks.c.sink_id == KIND))
    else:
        row = {"project_id": project_id, "sink_id": KIND, "kind": "channel", "clearance": clearance}
        sql(db, insert(sinks).values(**row))


@pytest.fixture
def bridged(northwind) -> SimpleNamespace:
    """northwind's hub with a group chat of the owner and the project's agent, bridged to the group ROOM by a code
    the owner made from a web session."""
    client, db, headers = northwind.client, northwind.db, northwind.headers
    channel_sink(db)
    chat = created(client, headers["owner"], title="Bridged", agents=[PROJECT])
    made = bridge_code(client, chat["id"], northwind.session.write)
    assert made.status_code == 201, made.text
    assert core.said(client, text(f"/bridge {made.json()['code']}", group_id=ROOM)) == "bridged"
    ((bridge_id,),) = sql(db, select(tables.chat_bridges.c.id))
    return SimpleNamespace(**vars(northwind), chat=chat, bridge_id=bridge_id, code=made.json()["code"])


def out(fake) -> list[dict]:
    return fake.to(ROOM, EVERYONE)


@on_pg
def test_a_chat_admin_bridges_a_group_with_a_code_from_a_web_session_alone(northwind):
    client, db, headers = northwind.client, northwind.db, northwind.headers
    channel_sink(db)
    chat = created(client, headers["owner"], title="Bridged", agents=[PROJECT])
    assert bridge_code(client, chat["id"], headers["owner"]).status_code == 403  # a machine token
    assert bridge_code(client, chat["id"], northwind.session.write, kind="matrix").status_code == 422
    alone = created(client, headers["owner"], title="No agents")
    assert bridge_code(client, alone["id"], northwind.session.write).status_code == 409
    theirs = core.web(client, db, OTHER)
    assert bridge_code(client, chat["id"], theirs.write).status_code in (403, 404)  # not in the chat
    made = bridge_code(client, chat["id"], northwind.session.write)
    assert made.status_code == 201, made.text
    code = made.json()["code"]
    assert core.said(client, text(f"/bridge {'x' * 32}", group_id=ROOM)) == "bridge_failed"
    assert core.said(client, text(f"/bridge {code}", group_id=ROOM)) == "bridged"
    assert core.said(client, text(f"/bridge {code}", group_id="room-8")) == "bridge_failed"  # once
    assert bridge_rows(db) == [(KIND, ROOM, None)]
    shown = client.get(f"/v1/chats/{chat['id']}", headers=headers["owner"]).json()
    assert [(b["kind"], b["external_id"], b["linked_by"]) for b in shown["bridges"]] == [(KIND, ROOM, OWNER)]
    actions = [row[0] for row in audit_rows(db, "chat")]
    assert "chat.bridge" in actions
    # A group bridges one chat at a time.
    other = created(client, headers["owner"], title="Another", agents=[PROJECT])
    again = bridge_code(client, other["id"], northwind.session.write).json()["code"]
    assert core.said(client, text(f"/bridge {again}", group_id=ROOM)) == "refused"


@on_pg
def test_a_bridged_groups_message_comes_in_as_the_linked_member_and_goes_back_to_no_one(bridged):
    client, fake, chat = bridged.client, bridged.fake, bridged.chat
    assert core.said(client, text("ship it", group_id=ROOM, message_id="n-1")) == "bridged_in"
    assert core.said(client, text("ship it", group_id=ROOM, message_id="n-1")) == "bridged_in"  # idempotent
    assert core.said(client, text("who am I", group_id=ROOM, user_id="u-999", message_id="n-2")) == "not_linked"
    messages = page(client, bridged.headers["owner"], chat["id"])["messages"]
    came = [m for m in messages if m["body"] == "ship it"]
    assert len(came) == 1 and came[0]["via"] == KIND and came[0]["author"]["login"] == OWNER, came
    core.deliver_events(client)
    assert [m["text"] for m in out(fake)] == [], "no echo to the group it came from"


@on_pg
def test_a_hub_message_goes_out_redacted_threaded_and_once_while_the_channel_sink_clears_it(bridged):
    client, db, fake, chat, headers = bridged.client, bridged.db, bridged.fake, bridged.chat, bridged.headers
    assert core.said(client, text("first", group_id=ROOM, message_id="n-1")) == "bridged_in"
    m = tables.chat_messages
    ((first,),) = sql(db, select(m.c.id).where(m.c.chat_id == chat["id"], m.c.body == "first"))
    answer = sent(client, headers["owner"], chat["id"], "token API_TOKEN=abc123secret here", reply_to=first)
    core.deliver_events(client)
    (line,) = out(fake)
    assert line["text"] == f"{OWNER}: token API_TOKEN=*** here" and line["reply_to"] == "n-1", line
    core.deliver_events(client)
    assert len(out(fake)) == 1, "once"
    shown = page(client, headers["owner"], chat["id"])["messages"]
    (mine,) = [item for item in shown if item["id"] == answer["id"]]
    assert mine["bridged_to"] == [{"bridge_id": bridged.bridge_id, "kind": KIND}] and mine["via"] is None
    # A reply in the group to the message that went out threads under it on the hub.
    assert core.said(client, text("agreed", group_id=ROOM, message_id="n-3", reply_to=line["id"])) == "bridged_in"
    ((root,),) = sql(db, select(m.c.thread_root_id).where(m.c.body == "agreed"))
    assert root in (first, answer["id"])
    # The project's channel sink lowered below the chat's level: nothing goes out (fail closed).
    channel_sink(db, {"level": "public", "location": "any"})
    sent(client, headers["owner"], chat["id"], "kept in")
    core.deliver_events(client)
    assert [item["text"] for item in out(fake)][-1] == line["text"]


@on_pg
def test_a_bridge_carries_reactions_and_run_progress_of_the_messages_it_carried(bridged):
    client, db, fake, chat, headers = bridged.client, bridged.db, bridged.fake, bridged.chat, bridged.headers
    assert core.said(client, text("go", group_id=ROOM, message_id="n-1")) == "bridged_in"
    m = tables.chat_messages
    ((carried,),) = sql(db, select(m.c.id).where(m.c.body == "go"))
    reacted = client.post(
        f"/v1/chats/{chat['id']}/messages/{carried}/reactions", json={"emoji": "👍"}, headers=headers["owner"]
    )
    assert reacted.status_code in (200, 201), reacted.text

    async def moved(conn):
        return await chat_bridge_events.progress(conn, chat["id"], carried, 41, "running")

    assert core.on_hub(client, moved) == 1
    core.deliver_events(client)
    lines = [item["text"] for item in out(fake)]
    assert f"+👍 on message {carried} by person {OWNER}" in lines and "run 41: running" in lines, lines


@on_pg
def test_a_chat_bridge_reaction_names_who_put_it_a_person_or_an_agent(bridged):
    client, db, fake, chat = bridged.client, bridged.db, bridged.fake, bridged.chat
    assert core.said(client, text("look", group_id=ROOM, message_id="n-1")) == "bridged_in"
    m, cm, ap = tables.chat_messages, tables.chat_members, tables.agent_profiles
    ((carried,),) = sql(db, select(m.c.id).where(m.c.body == "look"))
    query = select(ap.c.id, ap.c.handle).join_from(cm, ap, ap.c.id == cm.c.profile_id).where(cm.c.chat_id == chat["id"])
    ((profile_id, handle),) = sql(db, query)

    async def done(conn):
        return await status_reaction(conn, message_id=carried, profile_id=profile_id, state="done")

    assert core.on_hub(client, done) == "✅"
    core.deliver_events(client)
    lines = [item["text"] for item in out(fake)]
    assert f"+✅ on message {carried} by agent {handle}" in lines, lines


@on_pg
def test_a_chat_bridge_carries_out_the_files_a_member_attached_and_reads_them_under_the_sink(bridged, s3, monkeypatch):
    client, db, fake, chat, headers = bridged.client, bridged.db, bridged.fake, bridged.chat, bridged.headers
    config = replace(client.app.state.config, **s3.config())
    store = BlobStore.from_config(config)
    monkeypatch.setattr(client.app.state, "config", config)
    monkeypatch.setattr(client.app.state, "blobs", store)
    target = f"/v1/chats/{chat['id']}/attachments"
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    try:
        up = client.post(
            target, params={"name": "plan.png"}, content=png, headers={**headers["owner"], "Content-Type": "image/png"}
        )
        assert up.status_code == 201, up.text
        attachment_id = up.json()["id"]
        message = {"body": "the plan", "attachment_ids": [attachment_id]}
        posted = client.post(f"/v1/chats/{chat['id']}/messages", json=message, headers=headers["owner"])
        assert posted.status_code == 201, posted.text
        message_id = posted.json()["id"]
        other = sent(client, headers["owner"], chat["id"], "no files here")
        core.deliver_events(client)
        (line,) = [item for item in out(fake) if item["text"].endswith("the plan")]
        (carried,) = line["files"]
        assert (carried["name"], carried["size"], base64.b64decode(carried["data"])) == ("plan.png", len(png), png)

        mine = api.plugin("northwind_chat.server")

        async def read(conn):
            return await api.bridge_message(conn, mine, bridged.bridge_id, message_id)

        found = core.on_hub(client, read)
        assert found.files == (api.BridgeFile(attachment_id, "plan.png", "image/png", len(png)),)
        context = api.Context(mine.id, client.app.state.engine, config)

        def file_of(of_message: int, file_id: int = attachment_id):
            return client.portal.call(api.bridge_file, context, mine, bridged.bridge_id, of_message, file_id)

        assert file_of(message_id) == png
        assert file_of(other["id"]) is None, "a file of another message"
        # The project's channel sink lowered below the chat's level: neither the message nor its file goes out.
        channel_sink(db, {"level": "public", "location": "any"})
        assert file_of(message_id) is None
        assert core.on_hub(client, read) is None
    finally:
        store.close()


@on_pg
def test_a_bridge_unlinks_from_the_cli_the_group_and_a_hub_admin(bridged):
    client, db, headers, chat = bridged.client, bridged.db, bridged.headers, bridged.chat
    path = f"/v1/chats/{chat['id']}/bridges/{bridged.bridge_id}"
    assert client.delete(f"/v1/chats/{chat['id']}/bridges/999999", headers=headers["owner"]).status_code == 404
    gone = client.delete(path, headers=headers["owner"])
    assert gone.status_code == 200 and gone.json()["bridges"] == [], gone.text
    assert bridge_rows(db) == [(KIND, ROOM, "cli")]
    assert core.said(client, text("/unbridge", group_id=ROOM)) == "not_bridged"
    assert core.said(client, text("after", group_id=ROOM, message_id="n-9")) != "bridged_in"
    # Bridged again, the group takes it off; bridged a third time, a hub admin does.
    for via in ("bot", "admin"):
        made = bridge_code(client, chat["id"], bridged.session.write).json()["code"]
        assert core.said(client, text(f"/bridge {made}", group_id=ROOM)) == "bridged"
        if via == "bot":
            assert core.said(client, text("/unbridge", group_id=ROOM)) == "unbridged"
        else:
            ((bridge_id,),) = sql(
                db, select(tables.chat_bridges.c.id).where(tables.chat_bridges.c.unlinked_at.is_(None))
            )
            admin_path = f"/v1/northwind-chat/admin/bridges/{bridge_id}"
            assert client.delete(admin_path, headers=headers["owner"]).status_code == 403
            done = client.delete(admin_path, headers=headers["admin"])
            assert done.status_code == 200 and done.json() == {"unbridged": True}
    assert [row[2] for row in bridge_rows(db)] == ["cli", "bot", "admin"]
    a = tables.audit
    unbridged = sql(db, select(a.c.target).where(a.c.action == "chat.unbridge").order_by(a.c.id))
    unbridged = [target for (target,) in unbridged]  # the group's own unlink names no actor
    assert [target.rsplit(" ", 1)[1] for target in unbridged] == ["via=cli", "via=bot", "via=admin"]


@on_pg
def test_the_api_reads_a_bridge_of_its_own_plugin_alone(bridged):
    client = bridged.client

    async def read(conn):
        mine, theirs = api.plugin("northwind_chat.server"), api.plugin("lumen_relay")
        return (
            await api.bridge_find(conn, mine, KIND, ROOM),
            await api.bridge_read(conn, theirs, bridged.bridge_id),
        )

    found, other = core.on_hub(client, read)
    assert found is not None and found.chat_id == bridged.chat["id"] and found.external_id == ROOM
    assert other is None


@on_pg
def test_hub_chat_show_lists_the_bridges_and_hub_chat_unbridge_takes_one_off(bridged, monkeypatch, capsys):
    import argparse
    import json

    from evo_agents.hub import cli_chats
    from evo_agents.hub.client import HubError

    client, chat = bridged.client, bridged.chat

    class Hub:
        def call(self, method, path, body=None):
            answer = client.request(method, path, json=body, headers=bridged.headers["owner"])
            if answer.status_code >= 400:
                raise HubError(answer.json()["message"], answer.status_code, answer.json()["error"])
            return answer.json()

    monkeypatch.setattr(cli_chats, "signed_in", lambda: (Hub(), None))
    assert core.said(client, text("from the group", group_id=ROOM, message_id="n-1")) == "bridged_in"
    assert cli_chats.cmd_show(argparse.Namespace(chat=chat["id"], thread=None, limit=5, json=False)) == 0
    shown = capsys.readouterr().out
    assert f"bridged to: #{bridged.bridge_id} {KIND} {ROOM}" in shown and f"from {KIND}" in shown, shown
    assert cli_chats.cmd_unbridge(argparse.Namespace(chat=chat["id"], bridge=bridged.bridge_id, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["bridges"] == []
    assert cli_chats.cmd_unbridge(argparse.Namespace(chat=chat["id"], bridge=bridged.bridge_id, json=True)) != 0
    assert f"has no bridge {bridged.bridge_id}" in capsys.readouterr().err
    assert bridge_rows(bridged.db) == [(KIND, ROOM, "cli")]
