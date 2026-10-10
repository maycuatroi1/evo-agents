"""The parts of ``evo_agents.worker.power``: the daemon keeps the machine awake while it holds runs, and notices a
sleep all the same.

The checks step 1 of the run-reliability plan names: on macOS the daemon holds a power assertion while it holds at
least one run and lets it go when it holds none and when it stops; on Linux, and when the assertion cannot be made, it
logs once and goes on; a turn of a loop later on the wall clock than on the monotonic one by more than 30 seconds logs
"wake detected" with the gap, and the daemon claims no new run for 120 seconds while the heartbeats of the run it holds
go on (``tests.worker.test_power_daemon``, the daemon itself). The clocks and IOKit are fakes here, so nothing sleeps;
one test makes a real assertion on a Mac, and powerd's release of a dead process's assertions was checked by hand
(docs/workers.md). Standard library only: these run without the worker extra and without Postgres."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time

import pytest

from evo_agents.worker import power
from evo_agents.worker.power import (
    ASSERTION_TYPES,
    LEVEL_ON,
    KeepAwake,
    MacPower,
    PowerUnavailable,
    WakeWatch,
    power_for,
)

NO_ASSERTION = "no power assertion: the machine may sleep while the worker holds runs"


class FakePower:
    """Power assertions that record what is asked; ``fail`` makes ``hold`` refuse with that reason."""

    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.calls: list[tuple] = []
        self._ids = iter(range(1, 1000))

    def hold(self, name: str) -> list[int]:
        self.calls.append(("hold", name))
        if self.fail:
            raise PowerUnavailable(self.fail)
        return [next(self._ids) for _ in ASSERTION_TYPES]

    def release(self, held: list[int]) -> None:
        self.calls.append(("release", list(held)))

    @property
    def holding(self) -> bool:
        holds = sum(1 for call in self.calls if call[0] == "hold")
        return holds > sum(1 for call in self.calls if call[0] == "release")


class Clocks:
    """A wall clock and a monotonic clock that go with real time, each moved by hand on top of it: a sleep moves the
    wall clock alone, as the machine's own clocks do."""

    def __init__(self):
        self.wall_offset = 0.0
        self.monotonic_offset = 0.0

    def wall(self) -> float:
        return time.time() + self.wall_offset

    def monotonic(self) -> float:
        return time.monotonic() + self.monotonic_offset

    def sleep(self, seconds: float) -> None:
        self.wall_offset += seconds

    def awake(self, seconds: float) -> None:
        self.wall_offset += seconds
        self.monotonic_offset += seconds


def records(caplog, message: str) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.getMessage() == message]


# The power assertion


def test_the_power_assertion_is_held_while_a_run_is_held_and_released_when_none_is(caplog):
    caplog.set_level(logging.INFO, logger="evo_agents.worker")
    fake = FakePower()
    awake = KeepAwake("mac-mini", power=lambda: fake)
    awake.update(0)
    assert fake.calls == [] and not awake.holding
    awake.update(1)
    assert fake.calls == [("hold", "evo-agents worker mac-mini: holding runs")] and awake.holding
    awake.update(2)
    awake.update(1)
    assert len(fake.calls) == 1, "one assertion for any number of runs"
    awake.update(0)
    assert fake.calls[-1] == ("release", [1, 2]) and not awake.holding
    awake.update(1)
    awake.close()
    assert fake.calls[-1] == ("release", [3, 4]) and not fake.holding
    awake.close()
    assert len(fake.calls) == 4, "closing twice lets go once"
    assert len(records(caplog, "power assertion held: the machine stays awake while the worker holds runs")) == 2
    assert len(records(caplog, "power assertion released: the worker holds no run")) == 2


def test_no_power_assertion_on_linux_is_logged_once_and_the_runs_go_on(caplog):
    with pytest.raises(PowerUnavailable, match="macOS only, not on linux"):
        power_for("linux")
    awake = KeepAwake("lab-02", power=lambda: power_for("linux"))
    for runs in (1, 0, 2, 0, 1):
        awake.update(runs)
        assert not awake.holding
    (said,) = records(caplog, NO_ASSERTION)
    assert said.levelno == logging.WARNING and said.reason.endswith("not on linux")
    awake.close()


def test_a_power_assertion_macos_refuses_is_logged_once_and_tried_again_with_the_next_run(caplog):
    fake = FakePower(fail="IOPMAssertionCreateWithName(PreventSystemSleep) returned 0xe00002c2")
    awake = KeepAwake("mac-mini", power=lambda: fake)
    awake.update(1)
    awake.update(0)
    awake.update(1)
    assert [call[0] for call in fake.calls] == ["hold", "hold"] and not awake.holding
    (said,) = records(caplog, NO_ASSERTION)
    assert "0xe00002c2" in said.reason
    fake.fail = None
    awake.update(0)
    awake.update(1)
    assert awake.holding, "a later run gets the assertion once macOS makes it"
    awake.close()


class FakeLibrary:
    """IOKit and CoreFoundation as ctypes loads them, for MacPower: each function records its calls."""

    def __init__(self, statuses: list[int] | None = None):
        self.calls: list[tuple] = []
        self.statuses = list(statuses or [])
        self.strings: dict[int, bytes] = {}
        self._next = iter(range(100, 10_000))

        def function(name, answer):
            def call(*args):
                self.calls.append((name, *args))
                return answer(*args)

            return call

        self.CFStringCreateWithCString = function("CFStringCreateWithCString", self._string)
        self.CFRelease = function("CFRelease", lambda ref: None)
        self.IOPMAssertionCreateWithName = function("IOPMAssertionCreateWithName", self._create)
        self.IOPMAssertionRelease = function("IOPMAssertionRelease", lambda ident: 0)

    def _string(self, allocator, text, encoding):
        assert allocator is None and encoding == power.UTF8
        ref = next(self._next)
        self.strings[ref] = text
        return ref

    def _create(self, kind, level, name, found):
        assert level == LEVEL_ON
        status = self.statuses.pop(0) if self.statuses else 0
        if status == 0:
            found._obj.value = next(self._next)
        return status

    def names(self, function: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == function]


def test_mac_power_makes_both_power_assertions_with_iokit_and_releases_them(monkeypatch):
    library = FakeLibrary()
    monkeypatch.setattr(power.ctypes, "CDLL", lambda path: library)
    mac = MacPower()
    held = mac.hold("evo-agents worker mac-mini: holding runs")
    created = library.names("IOPMAssertionCreateWithName")
    assert [library.strings[call[1]] for call in created] == [kind.encode() for kind in ASSERTION_TYPES]
    assert {library.strings[call[3]] for call in created} == {b"evo-agents worker mac-mini: holding runs"}
    made = [call[1] for call in library.names("CFStringCreateWithCString")]
    assert len(library.names("CFRelease")) == len(made) == 4, "every string made is released"
    mac.release(held)
    assert [call[1] for call in library.names("IOPMAssertionRelease")] == held and len(held) == 2


def test_mac_power_lets_the_first_assertion_go_when_the_second_is_refused(monkeypatch):
    library = FakeLibrary(statuses=[0, -536870206])
    monkeypatch.setattr(power.ctypes, "CDLL", lambda path: library)
    with pytest.raises(PowerUnavailable, match=r"PreventSystemSleep\) returned 0xe00002c2"):
        MacPower().hold("evo-agents worker mac-mini: holding runs")
    (released,) = library.names("IOPMAssertionRelease")
    assert released[1] == library.names("IOPMAssertionCreateWithName")[0][4]._obj.value


def test_mac_power_without_iokit_is_unavailable(monkeypatch):
    def missing(path):
        raise OSError(f"dlopen({path}) failed")

    monkeypatch.setattr(power.ctypes, "CDLL", missing)
    with pytest.raises(PowerUnavailable, match="IOKit could not be loaded"):
        MacPower()


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("pmset"), reason="IOKit and pmset are macOS's")
def test_a_real_power_assertion_on_this_mac_is_listed_under_the_process_and_released():
    awake = KeepAwake(f"pytest-{os.getpid()}")

    def listed() -> list[str]:
        shown = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=30).stdout
        return [line for line in shown.splitlines() if awake.name in line]

    awake.update(1)
    try:
        assert awake.holding
        lines = listed()
        assert len(lines) == 2 and all(f"pid {os.getpid()}(" in line for line in lines), lines
        assert {kind for kind in ASSERTION_TYPES if any(kind in line for line in lines)} == set(ASSERTION_TYPES)
    finally:
        awake.close()
    assert listed() == []


# Noticing a sleep


def test_a_turn_later_on_the_wall_clock_than_expected_by_over_30_seconds_is_a_wake(caplog):
    clocks = Clocks()
    wake = WakeWatch(wall=clocks.wall, monotonic=clocks.monotonic)
    assert wake.turn("heartbeat") is None, "a first turn has nothing to compare with"
    clocks.awake(15)
    assert wake.turn("heartbeat") is None and wake.hold_left() == 0
    clocks.sleep(29)
    clocks.awake(15)
    assert wake.turn("heartbeat") is None, "29 seconds more than expected is no sleep"
    clocks.awake(5)
    clocks.sleep(600)  # the lid was closed: the monotonic clock stopped
    clocks.awake(10)
    slept = wake.turn("heartbeat")
    assert slept == pytest.approx(600, abs=1)
    assert wake.hold_left() == pytest.approx(power.WAKE_HOLD_SECONDS, abs=1) == pytest.approx(120, abs=1)
    (said,) = records(caplog, "wake detected")
    assert said.levelno == logging.WARNING and said.loop == "heartbeat"
    assert said.gap_s == pytest.approx(615, abs=1) and said.slept_s == pytest.approx(600, abs=1)
    assert said.no_claims_for_s == 120

    # The claims wait 120 seconds from the wake, which another loop's first turn does not push back.
    assert wake.turn("claim") is None
    clocks.awake(30)
    assert wake.turn("claim") is None
    clocks.awake(60)
    assert wake.turn("claim") is None and wake.hold_left() == pytest.approx(30, abs=1)
    clocks.awake(31)
    assert wake.hold_left() == 0, "claims go on 120 seconds after the wake"
    assert wake.wakes == 1 and len(records(caplog, "wake detected")) == 1


def test_a_sleep_while_a_loop_waits_is_seen_by_each_loop_and_logged_once(caplog):
    clocks = Clocks()
    wake = WakeWatch(wall=clocks.wall, monotonic=clocks.monotonic)
    wake.turn("heartbeat")
    wake.turn("claim")
    clocks.awake(3)
    clocks.sleep(3600)
    clocks.awake(12)
    assert wake.turn("claim") == pytest.approx(3600, abs=1)
    assert wake.turn("heartbeat") == pytest.approx(3600, abs=1)
    assert wake.wakes == 1 and len(records(caplog, "wake detected")) == 1
    # A later sleep is another one.
    clocks.awake(200)
    wake.turn("heartbeat")
    clocks.sleep(45)
    assert wake.turn("heartbeat") == pytest.approx(45, abs=1)
    assert wake.wakes == 2
