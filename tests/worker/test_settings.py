"""The daemon takes the worker settings of the heartbeat answer (slots, name, labels) without a restart, in this
process against the in-memory hub of ``tests.worker.fake_hub``, without Postgres.

The checks of step 2 of the worker-settings-web plan: settings that differ from config.json change the config in
memory and on disk (0600, the other fields kept) with one log line; more slots are claimed at the next turn; fewer
stop no run, the daemon only claims nothing until it holds fewer runs than the new slots; an answer without settings
(a hub before schema 0026) changes nothing; the free slots a heartbeat reports go up to 16, and
``worker register --slots`` takes 1 to 16."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import stat

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import runs
from evo_agents.worker import cli as worker_cli
from evo_agents.worker.daemon import _settings
from tests.worker.fake_hub import FakeHub
from tests.worker.test_plan_runs import PLAN, machine, plan_body, wait_for, with_daemon  # noqa: F401 (machine)

CHANGED = "worker settings changed on the hub"


def records(caplog, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == message]


def plan_of_three() -> dict:
    body = plan_body()
    body["steps"][1].pop("depends_on")
    body["steps"].append({"id": 3, "title": "Alpha again", "repo": "alpha", "what": "write c.txt", "status": "pending"})
    return body


def waiting_steps(on, tmp_path, keys: dict[str, str]) -> dict[str, tuple]:
    """A scenario for each step key (key -> the file it writes) whose agent touches a file, then waits for another;
    key -> (started, release)."""
    files = {key: (tmp_path / f"started-{key}", tmp_path / f"release-{key}") for key in keys}
    on.scenarios(
        {
            key: [
                {"touch": str(files[key][0])},
                {"wait_for": str(files[key][1])},
                {"write": {name: "x\n"}},
                {"result": {"verify_commands": [f"test -f {name}"]}},
            ]
            for key, name in keys.items()
        }
    )
    return files


def on_disk(worker) -> dict:
    return json.loads(worker.home.config_path.read_text(encoding="utf-8"))


def test_more_slots_from_the_heartbeat_settings_are_claimed_without_a_restart(machine, tmp_path, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="evo_agents.worker")
    files = waiting_steps(machine, tmp_path, {"1": "a.txt", "2": "b.txt"})

    async def body(hub: FakeHub, daemon):
        before = on_disk(machine)
        assert daemon.config.slots == 1 and before["slots"] == 1
        first = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        await wait_for(files["1"][0].exists, "the first agent to start")
        second = hub.queue_step_run(PLAN, "2", "beta", "feat/beta")
        await asyncio.sleep(1.5)  # far longer than a claim takes here
        assert hub.runs[second]["state"] == "queued", "one slot, one run"
        assert hub.last_heartbeat["free_slots"] == 0

        hub.settings = {"slots": 2, "name": "mac", "labels": []}
        await wait_for(files["2"][0].exists, "the second agent to start, on the new slot")
        assert daemon.config.slots == 2
        after = on_disk(machine)
        assert after == {**before, "slots": 2}, "only slots changed; checkouts and the rest kept"
        assert stat.S_IMODE(machine.home.config_path.stat().st_mode) == 0o600
        (said,) = records(caplog, CHANGED)
        assert said.changed == {"slots": {"old": 1, "new": 2}}

        await asyncio.sleep(1.0)  # more heartbeats with the same settings: nothing new
        assert len(records(caplog, CHANGED)) == 1
        for key in ("1", "2"):
            files[key][1].write_text("go", encoding="utf-8")
        assert await hub.wait_state(first, "done", "failed") == "done", hub.texts(first)
        assert await hub.wait_state(second, "done", "failed") == "done", hub.texts(second)

    with_daemon(machine, body, plan=plan_of_three())


def test_fewer_slots_in_the_settings_stop_no_run_and_hold_the_claims_until_the_runs_held_fit(machine, tmp_path):  # noqa: F811
    files = waiting_steps(machine, tmp_path, {"1": "a.txt", "2": "b.txt", "3": "c.txt"})

    async def body(hub: FakeHub, daemon):
        hub.settings = {"slots": 2, "name": "mac", "labels": []}
        await wait_for(lambda: daemon.config.slots == 2, "two slots")
        one = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        two = hub.queue_step_run(PLAN, "2", "beta", "feat/beta")
        await wait_for(lambda: files["1"][0].exists() and files["2"][0].exists(), "both agents to start")

        hub.settings = {"slots": 1, "name": "mac", "labels": []}
        await wait_for(lambda: on_disk(machine)["slots"] == 1, "one slot, on disk")
        assert daemon.config.slots == 1
        three = hub.queue_step_run(PLAN, "3", "alpha", "feat/alpha")
        await asyncio.sleep(1.5)
        assert hub.runs[one]["state"] == "running" and hub.runs[two]["state"] == "running", "no run stopped"
        assert hub.runs[three]["state"] == "queued"

        files["1"][1].write_text("go", encoding="utf-8")
        assert await hub.wait_state(one, "done", "failed") == "done", hub.texts(one)
        await asyncio.sleep(1.5)
        assert hub.runs[three]["state"] == "queued", "one run held on one slot: no claim"

        files["2"][1].write_text("go", encoding="utf-8")
        assert await hub.wait_state(two, "done", "failed") == "done", hub.texts(two)
        await wait_for(files["3"][0].exists, "the third run, once the runs held fit the slot")
        files["3"][1].write_text("go", encoding="utf-8")
        assert await hub.wait_state(three, "done", "failed") == "done", hub.texts(three)

    with_daemon(machine, body, plan=plan_of_three())


def test_a_new_name_and_labels_in_the_settings_reach_config_json_and_free_slots_go_up_to_16(machine, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="evo_agents.worker")

    async def body(hub: FakeHub, daemon):
        hub.settings = {"slots": runs.MAX_SLOTS, "name": "mac-mini", "labels": ["gpu", "fast"]}
        await wait_for(lambda: on_disk(machine)["name"] == "mac-mini", "the new name on disk")
        saved = on_disk(machine)
        assert (saved["slots"], saved["labels"]) == (16, ["gpu", "fast"])
        assert (daemon.config.name, daemon.config.labels, daemon.config.slots) == ("mac-mini", ["gpu", "fast"], 16)
        await wait_for(lambda: hub.last_heartbeat["free_slots"] == 16, "a heartbeat with 16 free slots")
        (said,) = records(caplog, CHANGED)
        assert said.changed == {
            "slots": {"old": 1, "new": 16},
            "name": {"old": "mac", "new": "mac-mini"},
            "labels": {"old": [], "new": ["gpu", "fast"]},
        }

    with_daemon(machine, body)


def test_a_heartbeat_answer_without_settings_or_with_unreadable_ones_keeps_the_config(machine, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="evo_agents.worker")

    async def body(hub: FakeHub, daemon):
        before = machine.home.config_path.read_bytes()
        beats = hub.heartbeats
        await wait_for(lambda: hub.heartbeats >= beats + 3, "heartbeats from a hub that sends no settings")
        assert machine.home.config_path.read_bytes() == before and daemon.config.slots == 1

        hub.settings = {"slots": 17, "name": "mac", "labels": []}
        beats = hub.heartbeats
        await wait_for(lambda: hub.heartbeats >= beats + 3, "heartbeats with settings out of range")
        assert machine.home.config_path.read_bytes() == before and daemon.config.slots == 1
        assert len(records(caplog, "the hub sent worker settings this daemon cannot read")) == 1
        assert not records(caplog, CHANGED)

    with_daemon(machine, body)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"slots": 0, "name": "mac", "labels": []},
        {"slots": True, "name": "mac", "labels": []},
        {"slots": 2, "name": "", "labels": []},
        {"slots": 2, "name": "mac", "labels": [1]},
        {"slots": 2, "name": "mac"},
    ],
)
def test_settings_not_as_the_hub_sends_them_are_not_read(value):
    assert _settings(value) is None


def test_settings_as_the_hub_sends_them_are_read_and_unknown_keys_left():
    value = {"slots": 16, "name": "mac", "labels": ["gpu"], "later": 1}
    assert _settings(value) == {"slots": 16, "name": "mac", "labels": ["gpu"]}


def test_register_slots_takes_1_to_16():
    assert worker_cli.MAX_SLOTS == runs.MAX_SLOTS == 16
    assert worker_cli._slots("1") == 1 and worker_cli._slots("16") == 16
    for value in ("0", "17", "two"):
        with pytest.raises(argparse.ArgumentTypeError, match="from 1 to 16"):
            worker_cli._slots(value)
