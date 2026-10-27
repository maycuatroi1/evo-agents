"""Sinks of kind channel (criterion a1 of the plan chat-bridges): a project's knowledge.yaml declares, per kind of
channel of a plugin (``telegram`` say), the clearance of what of its chats may go out to a group of that channel; the
hub stores it at register (``tests.hub.test_projects``) and ``chat_access.channel_allows`` reads it. Without such a
sink for a kind, that channel gets nothing of the project's chats: it fails closed. The rule that a 1:1 private channel
gets minimal messages (``telegram.restricted``) reads the hub sink alone, as before.

The first tests need no Postgres; the last ones register projects (northwind and lumen, fake names) on the hub."""

from __future__ import annotations

import pytest

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
    from tests.hub.test_chats import OPEN_SINKS, agent, client, created, hub, on_hub, sent  # noqa: F401 (fixtures)
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
