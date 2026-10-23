"""The rules a core plugin adds to the Loop (extension point loop_rules, criterion a12 of plan autonomous-oss-growth),
on the hub with Postgres: the plugin growth of ``tests.hub.loop_plugin`` makes work items on a baseline, on the clock
and on an event of its own, through the path of the core ones (ancestors, one work item per key, the caps of its cycle,
the breaker); a plugin turned off in the project makes none and its open work items are cancelled; the core table of
rules stands as it was, and a project that does not enable the plugin is as before.
"""

from __future__ import annotations

from functools import partial
from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, update

from evo_agents.hub import hub_events, plugin_calls, tables
from evo_agents.hub.server.loop.dispatch import hand_out
from evo_agents.hub.server.loop.model import PLUGIN_KINDS
from evo_agents.hub.server.loop.tick import tick
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import loop_plugin
from tests.hub.live import sql
from tests.hub.test_ba_agent import enable, members  # noqa: F401 (members is a fixture)
from tests.hub.test_loop_tick import (  # noqa: F401 (the fixtures)
    CORE,
    app_key,
    client,
    hub,
    item,
    items,
    ledger,
    newest_event,
    policy,
    project_id,
    push_baseline,
    ticked,
)
from tests.hub.test_runs import PROJECT
from tests.hub.test_strategist_agent import CARD, edit

IDEA = f"growth:growth:ideas:{loop_plugin.WEEK}"


@pytest.fixture
def loop_core(monkeypatch):
    """The entry points of canvas, srs and tasks, and growth, the core plugin of ``tests.hub.loop_plugin``."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += [*CORE.values(), EntryPoint("growth", f"{loop_plugin.HERE}:MANIFEST", ENTRY_POINT_GROUP)]
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    loop_plugin.CALLS.clear()
    loop_plugin.MODE.clear()
    yield
    plugin_calls.core_registry.cache_clear()


async def _plugin_hand_out(conn, project_id, found, view, worker):
    """The hand-out of the work items of a plugin's kind alone: the core ones stay queued, as ``ticked`` leaves them."""
    return await hand_out(conn, project_id, found, view, worker) if found.kind in PLUGIN_KINDS else None


def handing(client, db) -> dict:
    """One tick with the hand-out of the plugin's kinds."""
    return client.portal.call(partial(tick, client.app.state.engine, project_id(db), dispatch=_plugin_hand_out))


def plugin_items(db) -> list[tuple]:
    return [row for row in items(db) if row[1] in PLUGIN_KINDS]


def test_loop_rules_plugin_contributes_a_rule_and_its_work_item_runs_once_through_admission(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True})
    handing(client, hub_db)
    assert plugin_items(hub_db) == [(IDEA, "growth_idea", "running", None)]  # a job: running with no run
    made = item(hub_db, IDEA)
    assert (made["engine"], made["cycle"], made["run_id"], made["attempts"]) == ("plugin/growth", "business", None, 1)
    assert made["detail"] == {"week": loop_plugin.WEEK, "plugin": "growth", "rule": "ideas"}
    assert [row[1:] for row in ledger(hub_db) if row[0] == IDEA] == [
        ("created", "queued", None),
        ("dispatched", "running", None),
    ]
    before = len(items(hub_db))
    handing(client, hub_db)  # the same delta read again makes nothing more: its key is taken
    assert len(items(hub_db)) == before
    # the handler is called with its own work items alone, and the clock has no event
    last = loop_plugin.CALLS[-1]
    assert last["event"] is None and [found["key"] for found in last["items"]] == [IDEA]


def test_loop_rules_plugin_work_item_of_an_event_has_its_ancestors_and_the_core_row_3_is_unchanged(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True})
    handing(client, hub_db)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    handing(client, hub_db)
    kinds = sorted(kind for _, kind, _, _ in items(hub_db))
    assert kinds == ["brainstorm", "growth_idea", "growth_idea", "use_case"]  # core row 3 still makes its use_case
    baseline = next(row for row in plugin_items(hub_db) if row[0].startswith("growth:growth:baseline:"))
    made = item(hub_db, baseline[0])
    assert made["goal_id"] == item(hub_db, "brainstorm:G-1:r1")["goal_id"] and made["event_id"] is not None


def test_loop_rules_plugin_work_items_count_against_the_concurrency_cap_and_the_rate_of_their_cycle(
    client, hub, hub_db
):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True, "business": {"max_running": 1}})
    handing(client, hub_db)
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    handing(client, hub_db)
    states = {key: (state, reason) for key, kind, state, reason in plugin_items(hub_db)}
    assert sorted(states.values()) == [("queued", "concurrency"), ("running", None)]
    policy(client, hub, {"enabled": True, "business": {"max_running": 5}, "dispatches_per_hour": 1})
    handing(client, hub_db)
    assert sorted(state for state, _ in {key: (s, r) for key, _, s, r in plugin_items(hub_db)}.values()) == [
        "queued",
        "running",
    ]
    assert [reason for _, _, state, reason in plugin_items(hub_db) if state == "queued"] == ["rate"]


def test_loop_rules_plugin_work_items_wait_while_the_breaker_is_tripped(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True})
    s = tables.loop_states
    sql(hub_db, update(s).values(breaker="tripped", breaker_reason="too many work items", tripped_at=func.now()))
    handing(client, hub_db)
    assert plugin_items(hub_db) == [(IDEA, "growth_idea", "queued", "breaker")]


def test_loop_rules_plugin_task_kind_without_its_agent_type_stays_queued_with_the_hubs_why(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    loop_plugin.MODE["draft"] = True
    policy(client, hub, {"enabled": True})
    handing(client, hub_db)
    draft = item(hub_db, f"growth:growth:draft:{loop_plugin.WEEK}")
    assert (draft["state"], draft["reason"], draft["run_id"]) == ("queued", "type_refused", None)
    assert draft["detail"]["why"]


def test_loop_rules_plugin_moves_its_own_work_item_and_leaves_out_what_it_may_not_make(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True})
    handing(client, hub_db)
    core_item = item(hub_db, "brainstorm:G-1:r1")
    sql(
        hub_db,
        update(tables.loop_items)
        .where(tables.loop_items.c.key == IDEA)
        .values(detail={"plugin": "growth", "finish": True}),
    )
    loop_plugin.MODE["bad"] = core_item["id"]
    handing(client, hub_db)
    assert item(hub_db, IDEA)["state"] == "done"  # the plugin's own move
    assert item(hub_db, "brainstorm:G-1:r1")["state"] == "queued"  # a core work item is not the plugin's to move
    assert [kind for _, kind, _, _ in items(hub_db)] == [
        "brainstorm",
        "growth_idea",
    ]  # undeclared kind, foreign goal: out


def test_loop_rules_plugin_event_of_its_own_wakes_its_rule(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True})
    handing(client, hub_db)
    loop_plugin.CALLS.clear()
    kind = hub_events.own_kind("growth", "idea_seen")
    assert kind == "plugin.growth.idea_seen"
    engine = client.app.state.engine

    async def record():
        async with engine.begin() as conn:
            return await hub_events.record(conn, kind, project_id(hub_db), {"v": 1})

    client.portal.call(record)
    handing(client, hub_db)
    assert [call["event"]["kind"] for call in loop_plugin.CALLS if call["event"]] == [kind]


def test_loop_rules_plugin_turned_off_makes_no_work_and_its_open_work_items_are_cancelled(client, hub, hub_db):
    enable(client, hub["owner"], "growth")
    policy(client, hub, {"enabled": True, "business": {"max_running": 0}})
    handing(client, hub_db)
    assert plugin_items(hub_db) == [(IDEA, "growth_idea", "queued", "concurrency")]
    off = client.post(f"/v1/projects/{PROJECT}/plugins/growth/disable", headers=hub["owner"])
    assert off.status_code == 200, off.text
    loop_plugin.CALLS.clear()
    handing(client, hub_db)
    assert plugin_items(hub_db) == [(IDEA, "growth_idea", "cancelled", "plugin_off")]
    assert loop_plugin.CALLS == []  # a plugin that is off is not called
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-1")
    handing(client, hub_db)
    assert len(plugin_items(hub_db)) == 1  # and makes no work


def test_loop_rules_plugin_not_enabled_in_the_project_leaves_the_loop_as_it_was(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    handing(client, hub_db)
    assert [row[1] for row in items(hub_db)] == ["brainstorm"] and loop_plugin.CALLS == []
    assert newest_event(hub_db) is not None
