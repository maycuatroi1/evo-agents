"""The principal and the autonomy snapshot of a chat run (``evo_agents.hub.server.run_autonomy.of_message``), as
criterion a4 of plan autonomy-levels names it: the person who wrote the message that mentions the agent; for an agent
type that runs as a member, both, the stricter of them (an intern who mentions an agent run as their lead keeps the
intern's level); for a mention by an agent, the principal of that agent's run, within its snapshot.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.server import run_autonomy
from tests.hub.live import sql
from tests.hub.test_autonomy_level_runs import on_db, pref, user_id
from tests.hub.test_chat_runs import (  # noqa: F401 (app_key, client and hub are fixtures)
    OTHER,
    OWNER,
    PROJECT,
    app_key,
    chat_worker,
    client,
    created,
    hub,
    in_chats,
    run_of,
    sent,
    started,
)


def test_autonomy_level_principal_of_a_chat_run_is_the_writer_and_of_an_agent_mention_the_parent_principal(
    client, hub, hub_db
):
    pref(client, hub["owner"], "A4")
    worker = chat_worker(client, hub["owner"], "studio", slots=3)
    chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
    _, spec = started(client, hub, worker, chat)
    first = run_of(client, hub["owner"], spec["id"])
    assert (first["principal"], first["autonomy"]["level"], first["autonomy"]["also"]) == (OWNER, "A4", [])
    handed = in_chats(client, worker, spec["id"], "chat/mentions", {"handle": "fixer", "body": "fix the retry loop"})
    assert handed.status_code == 201, handed.text
    second = run_of(client, hub["owner"], handed.json()["run_id"])
    assert (second["principal"], second["autonomy"]["level"]) == (OWNER, "A4"), "within the run of the agent that asked"


def test_autonomy_level_principal_and_snapshot_of_run_as_intern_at_the_leads_agent_is_the_interns_level(
    client, hub, hub_db
):
    pref(client, hub["owner"], "A4")  # the lead, whom the agent type runs as
    pref(client, hub["other"], "A1")  # the intern
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])
    mention = sent(client, hub["other"], chat["id"], "@reviewer look at the flaky test")
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    lead, intern = user_id(hub_db, OWNER), user_id(hub_db, OTHER)
    values = {"kind": "chat", "project_id": project_id, "dispatched_by": lead}

    async def stamped(conn):
        who = await run_autonomy.of_message(conn, mention["id"], lead, intern)
        return who, await run_autonomy.stamp(conn, values, who)

    who, row = on_db(hub_db, stamped)
    assert who == run_autonomy.For(user_id=intern, also=(lead,))
    held = row["autonomy"]
    assert (row["principal_id"], held["level"], held["level_source"], held["also"]) == (
        intern,
        "A1",
        f"pref:{OTHER}",
        [OWNER],
    )
    assert held["restricted"] and held["classes"]["K6"]["mode"] == "deny"
    # the run_as alone, without the intern, would have been A4
    alone = on_db(hub_db, lambda conn: run_autonomy.stamp(conn, values, run_autonomy.For(user_id=lead)))
    assert alone["autonomy"]["level"] == "A4"
