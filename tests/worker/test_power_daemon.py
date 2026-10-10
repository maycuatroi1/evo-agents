"""The daemon keeps the machine awake while it holds a run and holds its claims after a sleep, in this process against
the in-memory hub of ``tests.worker.fake_hub``, without Postgres; ``tests.worker.test_power`` checks the parts.

The checks step 1 of the run-reliability plan names: the power assertion is held while the daemon holds a run and let
go when it holds none and when it stops; without one the daemon logs once and runs its runs; after a wake (a fake wall
clock moved 20 minutes ahead of the monotonic one) it claims nothing for 120 seconds while the heartbeats of the run it
holds go on, then claims again."""

from __future__ import annotations

import asyncio
import logging
import signal

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import power
from evo_agents.worker.power import KeepAwake, WakeWatch, power_for
from tests.worker.fake_hub import FakeHub
from tests.worker.test_plan_runs import PLAN, machine, wait_for, with_daemon  # noqa: F401 (machine, a fixture)
from tests.worker.test_power import NO_ASSERTION, Clocks, FakePower, records


def step_run_waiting_for(on, release, started) -> None:
    on.scenarios(
        {
            "1": [
                {"touch": str(started)},
                {"wait_for": str(release)},
                {"write": {"a.txt": "a\n"}},
                {"result": {"verify_commands": ["test -f a.txt"]}},
            ]
        }
    )


def test_the_daemon_holds_the_power_assertion_while_it_holds_a_run_and_releases_it_after(machine, tmp_path):  # noqa: F811
    fake = FakePower()
    awake = KeepAwake("mac", power=lambda: fake)
    release, started = tmp_path / "release", tmp_path / "started"
    step_run_waiting_for(machine, release, started)

    async def body(hub: FakeHub, daemon):
        assert daemon.keep_awake is awake and not fake.holding, "no run, no assertion"
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "the agent to start")
        assert fake.holding and fake.calls == [("hold", "evo-agents worker mac: holding runs")]
        release.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed") == "done", hub.texts(run_id)
        await wait_for(lambda: not fake.holding, "the assertion to go with the run")
        assert [call[0] for call in fake.calls] == ["hold", "release"]

    with_daemon(machine, body, keep_awake=awake)
    assert not fake.holding


def test_a_daemon_stopped_while_it_holds_a_run_releases_the_power_assertion(machine, tmp_path):  # noqa: F811
    fake = FakePower()
    release, started = tmp_path / "release", tmp_path / "started"
    step_run_waiting_for(machine, release, started)

    async def body(hub: FakeHub, daemon):
        hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "the agent to start")
        assert fake.holding
        daemon._signal(signal.SIGTERM)
        daemon._signal(signal.SIGTERM)  # the second stops the agent now, and its run fails

    with_daemon(machine, body, keep_awake=KeepAwake("mac", power=lambda: fake))
    assert not fake.holding and fake.calls[-1][0] == "release"


def test_a_daemon_without_a_power_assertion_logs_once_and_runs_its_runs(machine, tmp_path, caplog):  # noqa: F811
    machine.scenarios({"1": [{"write": {"a.txt": "a\n"}}, {"result": {"verify_commands": ["test -f a.txt"]}}]})
    awake = KeepAwake("lab-02", power=lambda: power_for("linux"))

    async def body(hub: FakeHub, daemon):
        first = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        assert await hub.wait_state(first, "done", "failed") == "done", hub.texts(first)
        second = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        assert await hub.wait_state(second, "done", "failed") == "done", hub.texts(second)

    with_daemon(machine, body, keep_awake=awake)
    assert len(records(caplog, NO_ASSERTION)) == 1


def test_after_a_wake_the_daemon_claims_nothing_for_120_seconds_and_its_heartbeats_go_on(machine, tmp_path, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="evo_agents.worker")
    clocks = Clocks()
    wake = WakeWatch(wall=clocks.wall, monotonic=clocks.monotonic)
    release, started = tmp_path / "release", tmp_path / "started"

    async def body(hub: FakeHub, daemon):
        held = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(started.exists, "the agent to start")
        await wait_for(lambda: held in (hub.last_heartbeat or {}).get("runs", []), "a heartbeat naming the run")

        clocks.sleep(20 * 60)  # the machine slept 20 minutes in the middle of the run
        await wait_for(lambda: records(caplog, "no claims for a while after a sleep"), "the claims to hold")
        (said,) = records(caplog, "wake detected")
        assert said.gap_s >= 1200 and said.slept_s == pytest.approx(1200, abs=5)

        beats = hub.heartbeats
        queued = hub.queue_step_run(PLAN, "2", "beta", "feat/beta")
        release.write_text("go", encoding="utf-8")  # the held run ends; a slot is free
        assert await hub.wait_state(held, "done", "failed") == "done", hub.texts(held)
        await asyncio.sleep(1.5)  # far longer than a claim takes here (the fake hub answers within a second)
        assert hub.runs[queued]["state"] == "queued", "no claim within 120 seconds of the wake"
        assert hub.heartbeats >= beats + 3, "the heartbeats went on"

        clocks.awake(power.WAKE_HOLD_SECONDS)
        assert await hub.wait_state(queued, "leased", "running", "verifying", "done", "failed") != "queued"
        assert records(caplog, "claiming again after the sleep")
        assert wake.wakes == 1

    machine.scenarios(
        {
            "1": [
                {"touch": str(started)},
                {"wait_for": str(release)},
                {"write": {"a.txt": "a\n"}},
                {"result": {"verify_commands": ["test -f a.txt"]}},
            ],
            "2": [{"write": {"b.txt": "b\n"}}, {"result": {"verify_commands": ["test -f b.txt"]}}],
        }
    )
    with_daemon(machine, body, wake=wake)
