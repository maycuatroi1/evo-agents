"""The daemon takes the commit of nightly the hub read from its heartbeat answer, and carries out its owner's Update
now (criteria a2 and a3 of the nightly-within-a-minute plan), against the fake hub and the fake uv of
``tests.worker.test_self_update``, without Postgres.

- A read of the hub less than 10 minutes old, made after the daemon started, starts the update at the next turn,
  with no read of the tag; without one (a hub before 0052, an old read, a read before the start), the daemon reads
  the tag itself on its hourly schedule, on a clock that counts the time the machine sleeps.
- Update now: the daemon and the other daemons of its install claim no new run, the runs held end, the daemon
  updates; a failed update an hour ago does not hold it back; an update that fails, or that cannot happen (locked, run
  by hand, a daemon of the install that cannot update), ends the request with why, and claims start again.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import power
from evo_agents.worker.home import EXIT_UPDATED, PidLock, WorkerHome
from evo_agents.worker.selfupdate.supervisor import BY_HAND, Supervision
from tests.worker.test_plan_runs import PLAN, machine, wait_for  # noqa: F401 (machine)
from tests.worker.test_self_update import (  # noqa: F401 (fake)
    NEW,
    OLD,
    UNLOCKED,
    ended,
    fake,
    run_daemon,
    until_heartbeat,
    waiting_step,
)

REQUESTED = "2026-10-20T07:00:00.123456Z"
LATER_REQUEST = "2026-10-20T07:05:00.654321Z"


def hub_read(commit: str, *, ago: float = -30.0) -> dict:
    """The hub's read of nightly as an answer carries it, ``ago`` seconds old (by default a little ahead, so that it
    comes after the start of a daemon made later in the test)."""
    return {"commit": commit, "read_at": (datetime.now(UTC) - timedelta(seconds=ago)).isoformat()}


def idle_turn(updater) -> bool:
    return asyncio.run(updater.turn(held=0, orphans=0, worker="mac"))


@pytest.fixture
def home(tmp_path) -> WorkerHome:
    found = WorkerHome(tmp_path / "home")
    found.ensure()
    return found


# The hub's read of nightly


def test_a_fresh_read_of_the_hub_starts_the_update_at_the_next_turn_without_reading_the_tag(fake, home):
    updater = fake.updater(home, clock=fake.clock)
    try:
        updater.hear_lock(False)
        read = hub_read(NEW)
        updater.hear_answer(read, None)
        assert idle_turn(updater) is True, "installed in this one turn"
        assert fake.lookups == 0 and fake.install().commit == NEW
        assert updater.report()["nightly_commit"] == NEW
        assert updater.report()["checked_at"] == datetime.fromisoformat(read["read_at"]).isoformat()
    finally:
        updater.close()


def test_a_fresh_read_of_the_hub_of_the_commit_it_runs_says_current(fake, home):
    updater = fake.updater(home, clock=fake.clock)
    try:
        updater.hear_lock(False)
        updater.hear_answer(hub_read(OLD), None)
        assert idle_turn(updater) is False
        assert updater.state == "current" and fake.lookups == 0 and fake.calls() == []
        assert "update_now" not in updater.report(), "no request: nothing a hub before 0052 would not know"
    finally:
        updater.close()


@pytest.mark.parametrize(
    "read",
    [None, {"commit": NEW, "read_at": "not a time"}, "old", "before the start"],
    ids=["no-read", "unreadable", "older-than-10-minutes", "before-the-daemon-started"],
)
def test_without_a_fresh_read_of_the_hub_the_daemon_reads_the_tag_itself_hourly(fake, home, read):
    fake.point(OLD)
    if read == "old":
        read = hub_read(NEW, ago=11 * 60)
    elif read == "before the start":
        read = hub_read(NEW, ago=1)
    updater = fake.updater(home, clock=fake.clock)
    try:
        updater.hear_lock(False)
        updater.hear_answer(read, None)
        assert idle_turn(updater) is False
        assert fake.lookups == 1 and updater.state == "current" and updater.nightly == OLD
        assert idle_turn(updater) is False and fake.lookups == 1, "not before the hour is up"
        fake.later()
        assert idle_turn(updater) is False and fake.lookups == 2
    finally:
        updater.close()


def test_the_reads_of_the_tag_count_the_time_the_machine_sleeps(fake, home):
    updater = fake.updater(home)
    try:
        assert updater.clock is power.clock_through_sleep
    finally:
        updater.close()
    if hasattr(time, "CLOCK_BOOTTIME"):
        assert power._through_sleep_id("linux") == time.CLOCK_BOOTTIME
    assert power._through_sleep_id("darwin") == time.CLOCK_MONOTONIC
    assert power._through_sleep_id("win32") is None
    assert power.clock_through_sleep() > 0


def test_a_daemon_updates_on_the_hubs_read_with_no_read_of_the_tag(machine, fake):  # noqa: F811
    def updater(home):
        return fake.updater(home, clock=fake.clock)

    async def body(hub, daemon, task):
        hub.nightly = hub_read(NEW)
        assert await ended(task) == EXIT_UPDATED

    assert run_daemon(machine, body, updater=updater) == EXIT_UPDATED
    assert fake.install().commit == NEW and fake.lookups == 0


# Update now


def test_update_now_stops_the_claims_lets_the_run_held_end_and_updates(machine, fake, tmp_path):  # noqa: F811
    started, release = waiting_step(machine, tmp_path)
    fake.point(OLD)

    def updater(home):
        return fake.updater(home, clock=fake.clock)

    async def body(hub, daemon, task):
        hub.nightly = hub_read(OLD)
        await until_heartbeat(hub, lambda found: found.get("state") == "current", "current")
        first = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "the agent to start")
        fake.point(NEW)
        hub.nightly, hub.update_now = hub_read(NEW), {"requested_at": REQUESTED}
        report = await until_heartbeat(hub, lambda found: "update_now" in found, "the request heard")
        assert report["update_now"] == {"requested_at": REQUESTED, "ended": None}
        assert report["state"] == "waiting" and report["reason"] == "it holds 1 run(s)"
        await asyncio.sleep(1.5)  # a claim the request found in flight has ended
        second = hub.queue_step_run(PLAN, "2", "beta", "feat/beta")
        await asyncio.sleep(1.0)
        assert hub.runs[second]["state"] == "queued", "no new run under the request, with a free slot"
        assert not task.done() and fake.calls() == [], "no run is stopped, no code changes under it"
        release.write_text("go", encoding="utf-8")
        assert await hub.wait_state(first, "done", "failed") == "done", hub.texts(first)
        assert await ended(task) == EXIT_UPDATED
        assert hub.runs[second]["state"] == "queued"

    settings = {**UNLOCKED, "slots": 2}
    assert run_daemon(machine, body, updater=updater, settings=settings) == EXIT_UPDATED
    assert fake.install().commit == NEW and fake.lookups == 0


def test_update_now_tries_again_at_once_ends_failed_claims_again_and_a_new_request_updates(machine, fake, tmp_path):  # noqa: F811
    fake.steps(["fail", "fail"])
    started, release = waiting_step(machine, tmp_path)

    def updater(home):
        return fake.updater(home, clock=fake.clock)

    async def body(hub, daemon, task):
        hub.nightly = hub_read(NEW)
        await until_heartbeat(hub, lambda found: "error" in found, "the first update failed")
        assert len(fake.calls()) == 1
        hub.update_now = {"requested_at": REQUESTED}
        report = await until_heartbeat(
            hub, lambda found: (found.get("update_now") or {}).get("ended") == "failed", "the request failed"
        )
        assert report["update_now"] == {"requested_at": REQUESTED, "ended": "failed"}
        assert "uv could not install" in report["error"] and len(fake.calls()) == 2, "tried again within the hour"
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "a run claimed again while the hub still carries the request")
        release.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        await asyncio.sleep(0.6)
        assert len(fake.calls()) == 2, "the request it ended is not tried again"
        hub.update_now = {"requested_at": LATER_REQUEST}  # the hub closed the first, and the owner asks again
        assert await ended(task) == EXIT_UPDATED

    assert run_daemon(machine, body, updater=updater) == EXIT_UPDATED
    assert fake.install().commit == NEW and len(fake.calls()) == 4


def test_update_now_reads_the_tag_at_once_without_a_fresh_read_of_the_hub(fake, home):
    fake.point(OLD)
    updater = fake.updater(home, clock=fake.clock)
    try:
        updater.hear_lock(False)
        updater.hear_answer(None, None)
        assert idle_turn(updater) is False and fake.lookups == 1
        fake.point(NEW)
        assert idle_turn(updater) is False and fake.lookups == 1, "the hourly read is not due"
        updater.hear_answer(hub_read(NEW, ago=11 * 60), {"requested_at": REQUESTED})
        assert not updater.may_claim()
        assert idle_turn(updater) is True and fake.lookups == 2, "read at once, and updated"
    finally:
        updater.close()


@pytest.mark.parametrize("why", ["locked", "by hand"])
def test_an_update_now_that_cannot_happen_ends_impossible_and_claims_go_on(fake, home, why):
    supervision = Supervision(None, reason=BY_HAND) if why == "by hand" else None
    updater = fake.updater(home, clock=fake.clock, **({"supervision": supervision} if supervision else {}))
    try:
        updater.hear_lock(why == "locked")
        updater.hear_answer(hub_read(NEW), {"requested_at": REQUESTED})
        assert idle_turn(updater) is False
        report = updater.report()
        assert report["update_now"] == {"requested_at": REQUESTED, "ended": "impossible"}
        expected = ("locked", "its version is locked") if why == "locked" else ("unsupported", BY_HAND)
        assert (report["state"], report["reason"]) == expected
        assert updater.may_claim() and fake.calls() == []
    finally:
        updater.close()


def test_update_now_stops_the_claims_of_the_install_and_ends_when_a_daemon_of_it_cannot_update(fake, tmp_path):
    homes = [WorkerHome(tmp_path / name) for name in ("worker-a", "worker-b", "worker-c")]
    locks = []
    for home in homes:
        home.ensure()
        locks.append(PidLock(home.pid_path))
        assert locks[-1].acquire()
    a, b = fake.updater(homes[0], clock=fake.clock), fake.updater(homes[1], clock=fake.clock)
    c = fake.updater(homes[2], clock=fake.clock, supervision=Supervision(None, reason=BY_HAND))
    try:
        for updater in (a, b):
            updater.hear_lock(False)
        a.hear_answer(hub_read(NEW), {"requested_at": REQUESTED})
        assert not a.may_claim(), "the daemon asked claims nothing new"
        assert asyncio.run(a.turn(held=1, orphans=0, worker="a")) is False
        assert a.report()["reason"] == "it holds 1 run(s)"
        assert not b.may_claim(), "nor does another daemon of its install"
        b.idle()
        c.publish(held=0, orphans=0, worker="c")
        assert idle_turn(a) is False
        report = a.report()
        assert report["update_now"] == {"requested_at": REQUESTED, "ended": "impossible"}
        assert report["state"] == "waiting" and report["reason"] == f"worker c on the same install: {BY_HAND}"
        a.publish(held=0, orphans=0, worker="a")
        assert a.may_claim() and b.may_claim(), "the request ended: claims start again"
        a.idle()
        b.idle()
        a.hear_answer(hub_read(NEW), None)
        assert "update_now" not in a.report()
    finally:
        for updater in (a, b, c):
            updater.close()
        for lock in locks:
            lock.release()


def test_a_daemon_that_runs_nightly_already_claims_under_an_open_request(fake, home):
    """A daemon that restarted on the update takes the request again from the answer; until the hub sees it runs
    nightly's commit and ends it, the request holds back none of its claims."""
    updater = fake.updater(home, clock=fake.clock)
    try:
        updater.hear_lock(False)
        updater.hear_answer(hub_read(NEW), {"requested_at": REQUESTED})
        assert not updater.may_claim(), "a new daemon takes the open request from the answer"
        updater.idle()
        updater.hear_answer(hub_read(OLD), {"requested_at": REQUESTED})
        assert idle_turn(updater) is False and updater.state == "current"
        assert updater.may_claim()
    finally:
        updater.close()
