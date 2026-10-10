"""What keeps the machine awake while the daemon holds runs, and what notices that it slept all the same.

``KeepAwake``: on macOS the daemon holds two IOKit power assertions while it holds at least one run:
PreventUserIdleSystemSleep, so the machine does not sleep for want of a person at it, and PreventSystemSleep, so it
does not sleep at all while it runs on AC power (macOS does not honour that one on battery). The daemon lets them go
when it holds no run any more and when it stops, and powerd drops them when the daemon's process dies, SIGKILL
included. ``pmset -g assertions`` lists them under the daemon's pid, named ASSERTION_NAME with the worker's name. No
assertion keeps a laptop awake whose lid is closed on battery: it sleeps, the leases of its runs run out, and the hub
tries them again (``WakeWatch`` below, and the steady rule of ``evo_agents.hub.server.runs``). On Linux, and wherever
an assertion cannot be made, the daemon says so once in its log and goes on without one.

``WakeWatch``: each loop of the daemon begins a turn with ``turn``, which compares the gap since the loop's previous
turn on the wall clock (time.time) with the gap the loop expected, the one its own clock saw: the event loop's clock,
time.monotonic, stops while the machine sleeps (mach_absolute_time on macOS, CLOCK_MONOTONIC on Linux), so a gap
longer on the wall clock by more than WAKE_SLACK_SECONDS is a sleep. It logs "wake detected" with the gap once per
sleep, however many loops see it, and holds the daemon's claims for WAKE_HOLD_SECONDS from then (``hold_left``);
heartbeats go on. A forward step of the wall clock by more than the slack reads as a sleep too, which only delays
claims.

Standard library only: ctypes for IOKit and CoreFoundation.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger("evo_agents.worker")

ASSERTION_TYPES = ("PreventUserIdleSystemSleep", "PreventSystemSleep")  # kIOPMAssertionType*
ASSERTION_NAME = "evo-agents worker"  # with the worker's name, as pmset -g assertions shows it
MAX_NAME_CHARS = 128  # IOKit keeps an assertion's name to this
LEVEL_ON = 255  # kIOPMAssertionLevelOn
UTF8 = 0x08000100  # kCFStringEncodingUTF8
IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

WAKE_SLACK_SECONDS = 30.0  # a turn this much later on the wall clock than its loop expected is a sleep
WAKE_HOLD_SECONDS = 120.0  # no claim for this long after a sleep, while the network and the hub come back


class PowerUnavailable(Exception):
    """No power assertion can be made here, and why."""


class Power(Protocol):
    """Power assertions: ``hold`` makes them and returns what ``release`` lets go of."""

    def hold(self, name: str) -> list[int]: ...

    def release(self, held: list[int]) -> None: ...


class MacPower:
    """IOKit's power assertions (IOPMAssertionCreateWithName, IOPMAssertionRelease), through ctypes."""

    def __init__(self):
        try:
            iokit = ctypes.CDLL(ctypes.util.find_library("IOKit") or IOKIT)
            cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation") or CORE_FOUNDATION)
        except OSError as exc:
            raise PowerUnavailable(f"IOKit could not be loaded: {exc}") from exc
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        create = iokit.IOPMAssertionCreateWithName
        create.restype = ctypes.c_int32  # IOReturn
        create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        iokit.IOPMAssertionRelease.restype = ctypes.c_int32
        iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
        self._cf, self._iokit = cf, iokit

    def _string(self, text: str) -> int:
        made = self._cf.CFStringCreateWithCString(None, text.encode("utf-8"), UTF8)
        if not made:
            raise PowerUnavailable(f"CoreFoundation could not make the string {text!r}")
        return made

    def hold(self, name: str) -> list[int]:
        held: list[int] = []
        try:
            for kind in ASSERTION_TYPES:
                kind_ref = self._string(kind)
                try:
                    name_ref = self._string(name[:MAX_NAME_CHARS])
                    try:
                        found = ctypes.c_uint32(0)
                        status = self._iokit.IOPMAssertionCreateWithName(
                            kind_ref, LEVEL_ON, name_ref, ctypes.byref(found)
                        )
                    finally:
                        self._cf.CFRelease(name_ref)
                finally:
                    self._cf.CFRelease(kind_ref)
                if status != 0:
                    raise PowerUnavailable(f"IOPMAssertionCreateWithName({kind}) returned {status & 0xFFFFFFFF:#010x}")
                held.append(found.value)
        except BaseException:
            self.release(held)
            raise
        return held

    def release(self, held: list[int]) -> None:
        failed = [f"{ident}: {status & 0xFFFFFFFF:#010x}" for ident in held if (status := self._release(ident))]
        if failed:
            raise PowerUnavailable(f"IOPMAssertionRelease failed for {', '.join(failed)}")

    def _release(self, ident: int) -> int:
        return self._iokit.IOPMAssertionRelease(ident)


def power_for(platform: str | None = None) -> Power:
    """The power assertions of ``platform`` (this one by default); PowerUnavailable where it has none."""
    platform = platform or sys.platform
    if platform == "darwin":
        return MacPower()
    raise PowerUnavailable(f"the daemon makes power assertions on macOS only, not on {platform}")


class KeepAwake:
    """The power assertions of the daemon, held while it holds at least one run (``update``) and let go at its stop
    (``close``). Where they cannot be made, that is logged once and the daemon goes on."""

    def __init__(self, name: str, power: Callable[[], Power] = power_for):
        self.name = f"{ASSERTION_NAME} {name}: holding runs"[:MAX_NAME_CHARS]
        self._power_for = power
        self._power: Power | None = None
        self._held: list[int] | None = None
        self._said_unavailable = False

    @property
    def holding(self) -> bool:
        return self._held is not None

    def update(self, runs: int) -> None:
        """The daemon holds ``runs`` runs now: hold the assertions when it holds one, let them go when none. It never
        raises: a run starts or ends whatever becomes of the assertions."""
        try:
            if runs > 0 and self._held is None:
                self._hold()
            elif runs <= 0 and self._held is not None:
                self._release()
        except Exception:
            log.exception("keeping the machine awake failed")

    def close(self) -> None:
        self.update(0)

    def _hold(self) -> None:
        try:
            if self._power is None:
                self._power = self._power_for()
            self._held = self._power.hold(self.name)
        except Exception as exc:  # PowerUnavailable, or a library that answers otherwise than documented
            if not self._said_unavailable:
                self._said_unavailable = True
                log.warning(
                    "no power assertion: the machine may sleep while the worker holds runs",
                    extra={"reason": str(exc) or type(exc).__name__},
                )
            return
        log.info(
            "power assertion held: the machine stays awake while the worker holds runs",
            extra={"assertions": list(ASSERTION_TYPES), "assertion_name": self.name},
        )

    def _release(self) -> None:
        held, self._held = self._held, None
        try:
            self._power.release(held)
        except Exception as exc:
            log.warning("a power assertion was not released", extra={"error": str(exc) or type(exc).__name__})
            return
        log.info("power assertion released: the worker holds no run")


class WakeWatch:
    """Notices that the machine slept from the turns of the daemon's loops; ``wall`` and ``monotonic`` are the clocks
    (time.time and time.monotonic; tests pass their own)."""

    def __init__(
        self,
        *,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        slack: float = WAKE_SLACK_SECONDS,
        hold: float = WAKE_HOLD_SECONDS,
    ):
        self.wall, self.monotonic = wall, monotonic
        self.slack, self.hold = slack, hold
        self.wakes = 0  # sleeps noticed
        self._turns: dict[str, tuple[float, float]] = {}  # loop -> (wall, monotonic) at its last turn
        self._hold_until = float("-inf")  # on the monotonic clock
        self._slept_until = float("-inf")  # on the wall clock: the end of the last sleep logged

    def turn(self, loop: str) -> float | None:
        """Loop ``loop`` begins a turn. The seconds the machine slept since its previous turn when the wall clock
        passed the monotonic one by more than the slack, which holds claims for ``hold`` seconds from now; else
        None."""
        wall, monotonic = self.wall(), self.monotonic()
        previous = self._turns.get(loop)
        self._turns[loop] = (wall, monotonic)
        if previous is None:
            return None
        gap, expected = wall - previous[0], monotonic - previous[1]
        slept = gap - expected
        if slept <= self.slack:
            return None
        self._hold_until = max(self._hold_until, monotonic + self.hold)
        if previous[0] >= self._slept_until:  # no other loop logged this sleep
            self.wakes += 1
            log.warning(
                "wake detected",
                extra={
                    "loop": loop,
                    "gap_s": round(gap, 1),
                    "expected_s": round(expected, 1),
                    "slept_s": round(slept, 1),
                    "no_claims_for_s": self.hold,
                },
            )
        self._slept_until = max(self._slept_until, wall)
        return slept

    def hold_left(self) -> float:
        """Seconds until the daemon claims again after the last sleep noticed; 0 when it may claim now."""
        return max(0.0, self._hold_until - self.monotonic())
