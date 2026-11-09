"""A delivery to a plugin tried again when the plugin asks (``retry_after``), the pass that runs it scheduled for then,
and what a commit stores for a channel queued at once (``evo_agents.hub.plugin_events``, ``evo_agents.hub.jobs``)."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta

from sqlalchemy import Text, cast, column, func, select, table

from evo_agents.hub import jobs, plugin_calls, plugin_events, tables
from evo_agents.hub.plugin_calls import PluginCallError, PluginHostUnavailable, retry_after_of
from evo_agents.plugins.protocol import INTERNAL_ERROR
from tests.hub import live
from tests.hub.test_plugin_points import (  # noqa: F401 (fixtures)
    ASKED,
    _notify_owner,
    client,
    deliver,
    enable,
    fake,
    hub,
    on_hub,
)
from tests.hub.test_runs import add_worker, claim, dispatched


class Later(Exception):
    """An exception of a core plugin's own that asks for a later try, as the core plugin telegram's does."""

    def __init__(self, seconds):
        super().__init__("the group is at its pace")
        self.retry_after = seconds


ITEM = {"name": "bridge", "python": "tests.hub.nowhere:handler"}


def _call(monkeypatch, raised: Exception):
    async def handler(params, context):
        raise raised

    monkeypatch.setattr(plugin_calls, "load", lambda ref: handler)
    return asyncio.run(plugin_calls.call(None, "telegram", "core", "events", ITEM, {}, None, 1.0))


def test_a_core_plugin_exception_with_retry_after_asks_a_later_try_logged_without_traceback(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=plugin_calls.__name__)
    try:
        _call(monkeypatch, Later(4))
    except PluginCallError as exc:
        later = exc
    assert later.retry_after == 4.0 and "tried again in 4s: Later" in str(later)
    assert [record.exc_info for record in caplog.records] == [None]

    caplog.clear()
    try:
        _call(monkeypatch, RuntimeError("its own bug"))
    except PluginCallError as exc:
        broken = exc
    assert broken.retry_after is None  # no attribute: the backoff as before, and the traceback in the log
    assert [record.exc_info is not None for record in caplog.records] == [True]


def test_only_a_finite_number_of_at_least_zero_is_a_retry_after():
    assert retry_after_of(Later(2.5)) == 2.5 and retry_after_of(Later(0)) == 0.0
    for wrong in (True, -1, math.nan, math.inf, "3", None):
        assert retry_after_of(Later(wrong)) is None
    assert retry_after_of(RuntimeError()) is None


def _claim(attempts: int = 0, put_off: int = 0):
    return plugin_events._Claim(1, "telegram", "core", "events", ITEM, {}, attempts, None, put_off)


def _written(statement) -> dict:
    return statement.compile().params


def test_a_try_put_off_waits_what_the_plugin_asked_within_bounds_and_has_a_limit_of_its_own():
    assert plugin_events.put_off_for(0.2) == timedelta(seconds=1)
    assert plugin_events.put_off_for(7) == timedelta(seconds=7)
    assert plugin_events.put_off_for(10**6) == timedelta(seconds=plugin_events.BACKOFF_MAX_SECONDS)

    outcome, statement = plugin_events._settle(_claim(), None, "busy", 5.0)
    assert outcome == "retried" and (_written(statement)["attempts"], _written(statement)["put_off"]) == (1, 1)
    last = plugin_events.MAX_PUT_OFF - 1
    assert plugin_events._settle(_claim(last, last), None, "busy", 5.0)[0] == "failed"

    # Tries put off do not count against MAX_ATTEMPTS: a delivery put off 50 times still gets its failed tries.
    outcome, statement = plugin_events._settle(_claim(50, 50), None, "down")
    assert outcome == "retried" and (_written(statement)["attempts"], _written(statement)["put_off"]) == (51, 50)
    failed = plugin_events.MAX_ATTEMPTS - 1
    assert plugin_events._settle(_claim(50 + failed, 50), None, "down")[0] == "failed"


def _due_in(db) -> list[tuple]:
    d = tables.plugin_deliveries
    seconds = func.extract("epoch", d.c.next_at - func.now())
    return live.sql(db, select(d.c.state, d.c.attempts, d.c.put_off, seconds).order_by(d.c.id))


QUEUED = table(
    "procrastinate_jobs",
    column("task_name"),
    column("status"),
    column("queue_name"),
    column("queueing_lock"),
    column("scheduled_at"),
)


def _waiting(db, task: str) -> list[tuple]:
    query = select(QUEUED.c.queue_name, QUEUED.c.queueing_lock, QUEUED.c.scheduled_at).where(
        QUEUED.c.task_name == task, cast(QUEUED.c.status, Text) == "todo"
    )
    return live.sql(db, query.order_by(QUEUED.c.scheduled_at.nulls_first()))


def test_a_delivery_put_off_is_tried_when_the_plugin_asked_by_a_pass_scheduled_for_then(client, hub, hub_db, fake):
    enable(client, hub["owner"])
    worker = add_worker(client, hub["owner"], "mac-mini")
    dispatched(client, hub["owner"], [2])
    claim(client, worker)
    fake.answers["event"] = PluginCallError(INTERNAL_ERROR, "busy", retry_after=5)
    assert deliver(client)["retried"] == 1
    ((state, attempts, put_off, due_in),) = _due_in(hub_db)
    assert (state, attempts, put_off) == ("pending", 1, 1) and 3 < due_in <= 5
    d = tables.plugin_deliveries
    (next_at,) = live.sql(hub_db, select(d.c.next_at))[0]
    scheduled = [row for row in _waiting(hub_db, jobs.DELIVER_PLUGIN_EVENTS) if row[2] is not None]
    lock = jobs.at_lock(jobs.DELIVER_PLUGIN_EVENTS, next_at)
    assert scheduled == [(jobs.DELIVERY_QUEUE, lock, next_at)]  # not the lock of deliver_soon, which stays free

    # A plugin whose error asks nothing waits the backoff of a failed try, as before.
    fake.answers["event"] = PluginHostUnavailable("the backend is restarting")
    live.sql(hub_db, d.update().values(next_at=func.now()))
    assert deliver(client)["retried"] == 1
    ((state, attempts, put_off, due_in),) = _due_in(hub_db)
    assert (state, attempts, put_off) == ("pending", 2, 1) and 55 < due_in <= plugin_events.BACKOFF_SECONDS


def test_a_notification_queues_its_deliveries_at_once_on_their_own_queues(client, hub, hub_db, fake):
    assert _waiting(hub_db, jobs.DELIVER_NOTIFICATIONS) == []
    enable(client, hub["owner"], permissions=ASKED)
    on_hub(client, _notify_owner)
    on_hub(client, _notify_owner)
    task = jobs.DELIVER_NOTIFICATIONS
    assert _waiting(hub_db, task) == [(jobs.LEASE, task, None)]  # one waits, due now, and takes both
    soon = [row for row in _waiting(hub_db, jobs.DELIVER_PLUGIN_EVENTS) if row[1] == jobs.DELIVER_PLUGIN_EVENTS]
    assert [row[0] for row in soon] == [jobs.DELIVERY_QUEUE]  # the plugin's channel too
