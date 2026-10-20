"""A member's channel for the hub's tests of notifications: the sample core plugin northwind-chat
(``tests/fixtures/core_plugins/northwind_chat``) installed beside the core plugins of this process, Northwind Chat
faked (``tests/plugins/fake_northwind.py``), set up by a hub admin, and a member's chat linked from a live web session
of theirs, written straight into the database. The hub's Telegram channel is a core plugin of the repository
evo-agents-core-plugins, whose tests check what it sends.

The channel is cleared for internal in any location, so a project whose hub sink clears customer or domestic-only is
kept from it, as from Telegram's. northwind-chat sends a notice as ``<title>: <url>``, a decision as ``Decision #<id>:
<question>`` with a button per option, and anything of a project kept from it as ``Something of <project> waits for
you: <url>`` with no button (``northwind_chat.server.message``). ``outgoing`` holds each notification as the hub
handed it to the channel, ``extra`` included: what the hub let the channel send of it.
"""

from __future__ import annotations

import importlib
import secrets
from functools import partial
from importlib.metadata import EntryPoint
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from evo_agents.hub import plugin_calls, tables
from evo_agents.hub.server import notifications
from evo_agents.hub.server.sealing import KEY_BYTES
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.live import sql
from tests.plugins.fake_northwind import API_URL, GROUP, SECRET, TOKEN, USER, FakeNorthwind

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "core_plugins"
PLUGIN, KIND = "northwind-chat", "northwind"
NORTHWIND = EntryPoint(PLUGIN, "northwind_chat:manifest", ENTRY_POINT_GROUP)
CLEARANCE = {"level": "internal", "location": "any"}  # the channel's: below customer and domestic-only


@pytest.fixture
def northwind(monkeypatch) -> SimpleNamespace:
    """Northwind Chat in place of the real one (``chat``), northwind-chat installed beside the core plugins of this
    process, and the notifications its channel sent (``outgoing``)."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    server = importlib.import_module("northwind_chat.server")
    chat, outgoing = FakeNorthwind(), []
    monkeypatch.setattr(server, "TRANSPORT", chat.transport())
    send = server.NorthwindChannel.send

    async def sending(self, notification, config):
        outgoing.append(notification)
        return await send(self, notification, config)

    monkeypatch.setattr(server.NorthwindChannel, "send", sending)
    points = [*(point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name != PLUGIN), NORTHWIND]
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield SimpleNamespace(chat=chat, outgoing=outgoing)
    plugin_calls.core_registry.cache_clear()


def hub_config(db, tmp_path, github, **changes):
    """``live.hub_config`` with the web's sign-in and a key for secrets, which northwind-chat's config needs."""
    keyed = {**live.web_changes(github), "secrets_key": secrets.token_bytes(KEY_BYTES), **changes}
    return live.hub_config(db, tmp_path, github, **keyed)


def set_up(client, admin: dict) -> None:
    """northwind-chat configured by a hub admin (``admin``'s headers): Northwind Chat's API, the bot's token, the
    webhook's secret and the channel's clearance."""
    values = {"api_url": API_URL, "token": TOKEN, "webhook_secret": SECRET, "clearance": CLEARANCE}
    written = client.post(f"/v1/plugins/{PLUGIN}/config", json={"values": values}, headers=admin)
    assert written.status_code == 200, written.text


def linked(db, login: str) -> None:
    """A chat of ``login`` in Northwind Chat linked from a live web session of theirs."""
    _, session = live.web_session(db, login)
    users = tables.users
    ((user_id,),) = sql(db, select(users.c.id).where(users.c.login == login))
    channel = {"user_id": user_id, "kind": KIND, "config": {"group_id": GROUP, "user_id": USER}, "token_id": session}
    sql(db, insert(tables.notification_channels).values(**channel))


def cleared(db, clearance: dict | None = None) -> None:
    """The project's hub sink cleared for ``clearance``, internal for any location unless it says otherwise: within
    the channel's clearance, so the channel sends its notifications whole."""
    sinks = tables.project_sinks
    sql(db, update(sinks).values(clearance=clearance or CLEARANCE).where(sinks.c.kind == "hub"))


def deliver(client) -> dict:
    """One pass of the hub's delivery of notifications, on ``client``'s app."""
    engine, config = client.app.state.engine, client.app.state.config
    return client.portal.call(partial(notifications.deliver_notifications, engine, config=config))
