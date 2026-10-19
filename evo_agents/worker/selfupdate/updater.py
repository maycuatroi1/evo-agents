"""``SelfUpdate``: what the daemon decides, at each of its turns, of updating its install to the tag nightly, and what
its heartbeats say of it (``agent_commit``, and ``self_update`` with one of ``runs.SELF_UPDATE_STATES``).

A turn, in order:

1. Not an install it can update (``install``): nothing, state ``unsupported``.
2. It writes its state for the other daemons of the install (``peers``).
3. The install changed under it (another daemon of the install updated it, or a person did): once it holds no run
   and no daemon of the install is still updating it, it restarts on it; it claims nothing meanwhile. A daemon that
   may not update itself goes on as it is.
4. Turned off on this machine (EVO_WORKER_SELF_UPDATE=off), not run by launchd or systemd, or no uv: ``unsupported``.
5. The hub has not said whether the worker's version is locked: ``waiting``. Locked: ``locked``.
6. Where nightly points, read at the first turn, then hourly (after a failure, in 15 minutes): unknown, ``waiting``;
   the commit it runs, ``current``.
7. It holds runs or agents of a previous daemon, a daemon of the install is not ready (runs, a lock, run by hand),
   another one updates the install, or the update to that commit failed less than an hour ago: ``waiting``.
8. Else ``updating``: it closes the gate, waits until no daemon of the install claims, installs nightly
   (``installer``) and, once the new install starts, ends the daemon with ``home.EXIT_UPDATED``. A failure is kept as
   ``error`` and the daemon goes on with the code it runs.

Each change of state is one log line, "self-update: <state>", with the reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub import runs
from evo_agents.worker.home import WorkerHome
from evo_agents.worker.selfupdate.install import Install, find_uv, read_install
from evo_agents.worker.selfupdate.installer import Installer, Outcome, nightly_commit
from evo_agents.worker.selfupdate.peers import Peers, root_of
from evo_agents.worker.selfupdate.supervisor import BY_HAND, Supervision, detect

log = logging.getLogger("evo_agents.worker")

OFF_VARIABLE = "EVO_WORKER_SELF_UPDATE"  # off (or 0, false, no): the daemons of this machine never update themselves
OFF_VALUES = ("off", "0", "false", "no")
CHECK_EVERY = 3600.0  # seconds between two reads of where nightly points
LOOKUP_RETRY = 900.0  # after a read that failed
INSTALL_RETRY = 3600.0  # before trying again an update to the commit whose update failed
IDLE_WAIT = 75.0  # seconds the updater waits for the claims of the install's daemons to end (a claim waits 25 + 20)
LOCK_UNKNOWN = "the hub has not said whether this worker's version is locked"
UV_MISSING = "uv is neither on the service's PATH nor in ~/.local/bin, ~/.cargo/bin, /opt/homebrew/bin, /usr/local/bin"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clip(text: str | None, most: int) -> str | None:
    return text[:most] if text else None


class SelfUpdate:
    def __init__(
        self,
        install: Install,
        *,
        supervision: Supervision | None = None,
        peers: Peers | None = None,
        installer: Installer | None = None,
        lookup: Callable[[], Awaitable[str]] | None = None,
        off: bool = False,
        clock: Callable[[], float] = time.monotonic,
        check_every: float = CHECK_EVERY,
        lookup_retry: float = LOOKUP_RETRY,
        install_retry: float = INSTALL_RETRY,
        idle_wait: float = IDLE_WAIT,
    ):
        self.install = install
        self.identity = install.identity()  # what lay in the install when the daemon started
        self.peers = peers if install.reason is None else None
        self.installer = installer
        self.lookup = lookup
        self.clock = clock
        self.check_every, self.lookup_retry, self.install_retry = check_every, lookup_retry, install_retry
        self.idle_wait = idle_wait
        self.static = self._static_reason(supervision, off)
        self.locked: bool | None = None  # as the hub said it in this daemon's life; None until it did
        self.state, self.reason = ("unsupported", self.static) if self.static else ("waiting", LOCK_UNKNOWN)
        self.nightly: str | None = None
        self.checked_at: str | None = None
        self.error: str | None = None
        self.error_at: str | None = None
        self.installed = False  # the install was updated: the daemon ends to restart on it
        self._error_of_lookup = False
        self._next_lookup = float("-inf")
        self._retry: dict[str, float] = {}  # commit -> when an update to it may be tried again
        self._task: asyncio.Future | None = None
        self._said: tuple | None = None
        self._change_said = False

    @classmethod
    def here(cls, home: WorkerHome, env: Mapping[str, str]) -> SelfUpdate:
        """The install this Python runs from, as the daemon of ``home`` with the environment ``env`` sees it."""
        install = read_install(Path(sys.prefix), getattr(sys, "_base_executable", None) or sys.executable)
        if install.reason is not None:
            return cls(install)
        uv = find_uv(env.get("PATH"))
        peers = Peers(root_of(install.prefix), home.root)
        return cls(
            install,
            supervision=detect(env),
            peers=peers,
            installer=Installer(install, uv, peers.staging, env, backup_dir=peers.backup) if uv else None,
            lookup=functools.partial(nightly_commit, env),
            off=(env.get(OFF_VARIABLE) or "").strip().lower() in OFF_VALUES,
        )

    def _static_reason(self, supervision: Supervision | None, off: bool) -> str | None:
        """Why this daemon never updates itself, whatever the hub says: a reason of its install or its machine."""
        if self.install.reason:
            return self.install.reason
        if off:
            return f"turned off on this machine ({OFF_VARIABLE})"
        if supervision is None or supervision.manager is None:
            return supervision.reason if supervision and supervision.reason else BY_HAND
        if supervision.reason:
            return supervision.reason
        if self.installer is None or self.lookup is None or self.peers is None:
            return UV_MISSING
        return None

    # What the heartbeat says

    def report(self) -> dict:
        found = {
            "state": self.state,
            "reason": _clip(self.reason, runs.SELF_UPDATE_REASON_CHARS),
            "nightly_commit": self.nightly,
            "checked_at": self.checked_at,
            "error": _clip(self.error, runs.SELF_UPDATE_ERROR_CHARS),
            "error_at": self.error_at,
        }
        return {key: value for key, value in found.items() if value is not None}

    def heartbeat_fields(self) -> dict:
        return {"agent_commit": self.install.commit, "self_update": self.report()}

    def hear_lock(self, value) -> None:
        """The lock the hub sent in a heartbeat answer's settings; None for an answer without it."""
        self.locked = value if isinstance(value, bool) else None

    # Claims

    def may_claim(self) -> bool:
        """Before each claim: False while another daemon of the install updates it, and once the install changed
        under this daemon when it restarts on it. The caller lets go with ``idle`` once it holds no run."""
        if self.peers is None:
            return True
        if self._changed() and self._may_restart():
            return False
        return self.peers.enter()

    def idle(self) -> None:
        if self.peers is not None:
            self.peers.idle()

    # Turns

    def _why_not(self, held: int, orphans: int) -> str | None:
        if self.static:
            return self.static
        if self.locked is None:
            return LOCK_UNKNOWN
        if self.locked:
            return "its version is locked"
        if held:
            return f"it holds {held} run(s)"
        if orphans:
            return "it has agents of a previous daemon to stop"
        return None

    def publish(self, *, held: int, orphans: int, worker: str) -> str | None:
        """Write this daemon's state for the others of the install; why it may not update now, or None."""
        why = self._why_not(held, orphans)
        if self.peers is not None:
            state = {"worker": worker, "commit": self.install.commit, "ready": why is None, "why": why}
            try:
                self.peers.publish({**state, "runs": held, "orphans": orphans})
            except OSError as exc:
                log.warning("self-update: the state for the other daemons of the install was not written", exc_info=exc)
        return why

    async def turn(self, *, held: int, orphans: int, worker: str) -> bool:
        """One turn of the daemon; True when it should stop now and exit EXIT_UPDATED."""
        if self.peers is None:
            return False
        why = self.publish(held=held, orphans=orphans, worker=worker)
        if self._changed():
            return self._restart_on_change(held == 0 and orphans == 0)
        if self.static:
            self._say("unsupported", self.static)
            return False
        if self.locked is None or self.locked:
            self._say("waiting" if self.locked is None else "locked", why)
            return False
        if self.clock() >= self._next_lookup:
            await self._look_up()
        target = self.nightly
        if target is None:
            self._say("waiting", "where nightly points is not known yet")
            return False
        if target == self.install.commit:
            if not self._error_of_lookup:
                self.error = self.error_at = None
            self._say("current", None)
            return False
        blocker = why or self._blocker(target)
        if blocker:
            self._say("waiting", blocker)
            return False
        return await self._update(target)

    async def after_outdated(self, stored_lock: bool | None) -> bool:
        """The hub answered 426 and the daemon's runs ended: update to nightly when the lock the hub sent last
        (config.json) says unlocked; True when the install was updated."""
        await self.settle()
        if self.installed:
            return True
        if self.peers is None or self.static:
            log.info(
                "self-update: the hub needs a newer daemon, and this one cannot update itself",
                extra={"reason": self.static},
            )
            return False
        if stored_lock is not False:
            why = "its version is locked" if stored_lock else LOCK_UNKNOWN
            log.info("self-update: the hub needs a newer daemon; not updating", extra={"reason": why})
            return False
        target = await self._look_up()
        if target is None:
            return False
        if target == self.install.commit:
            log.warning(
                "self-update: nightly is the commit this daemon runs; a newer one has to come first",
                extra={"commit": target},
            )
            return False
        blocker = self._peer_blocker()
        if blocker:
            log.info("self-update: the hub needs a newer daemon; not updating now", extra={"reason": blocker})
            return False
        return await self._update(target)

    async def settle(self) -> None:
        """Wait for an install in flight, which a stop does not cut short."""
        if self._task is not None and not self._task.done():
            with contextlib.suppress(Exception):
                await self._task

    def close(self) -> None:
        if self.peers is not None:
            self.peers.forget()
            self.peers.close()

    # The steps of a turn

    def _say(self, state: str, reason: str | None) -> None:
        self.state, self.reason = state, reason
        if (state, reason) != self._said:
            self._said = (state, reason)
            extra = {"reason": reason, "commit": self.install.commit, "nightly": self.nightly}
            log.info(f"self-update: {state}", extra=extra)

    def _changed(self) -> bool:
        return self.install.identity() != self.identity

    def _may_restart(self) -> bool:
        return self.static is None and self.locked is False

    def _restart_on_change(self, idle: bool) -> bool:
        now = self.install.identity()
        if not self._may_restart():
            if not self._change_said:
                self._change_said = True
                log.warning(
                    "self-update: the install changed under this daemon, which goes on with the code it started with",
                    extra={"now": now.dist_info, "commit": now.commit, "reason": self.static or "lock"},
                )
            return False
        if not idle:
            self._say("waiting", "the install was updated: it restarts on it once its runs end")
            return False
        if not self.peers.gate_open():  # uv may have deleted the environment, and the copy may yet go back
            self._say("waiting", "another daemon of this install is updating it")
            return False
        log.info("self-update: the install was updated; restarting on it", extra={"commit": now.commit})
        return True

    async def _look_up(self) -> str | None:
        try:
            commit = await self.lookup()
        except (LookupError, OSError) as exc:
            self._next_lookup = self.clock() + self.lookup_retry
            self.error, self.error_at, self._error_of_lookup = str(exc), _now(), True
            log.warning("self-update: where nightly points was not read", extra={"error": str(exc)})
            return None
        self._next_lookup = self.clock() + self.check_every
        self.checked_at = _now()
        if self._error_of_lookup:
            self.error = self.error_at = None
            self._error_of_lookup = False
        if commit != self.nightly:
            log.info("self-update: nightly read", extra={"nightly": commit, "commit": self.install.commit})
        self.nightly = commit
        return commit

    def _peer_blocker(self) -> str | None:
        for peer in self.peers.others():
            if not peer.get("ready"):
                return f"worker {peer.get('worker')} on the same install: {peer.get('why')}"
        return None

    def _blocker(self, target: str) -> str | None:
        retry = self._retry.get(target)
        if retry is not None and self.clock() < retry:
            return f"the update to {target[:12]} failed; it is tried again later"
        found = self._peer_blocker()
        if found:
            return found
        if not self.peers.gate_open():
            return "another daemon of this install is updating it"
        return None

    async def _update(self, target: str) -> bool:
        if not self.peers.close_gate():
            self._say("waiting", "another daemon of this install is updating it")
            return False
        keep = False  # the gate, until the daemon exits
        try:
            self._say("updating", "waiting for the daemons of this install to end their claims")
            if not await self.peers.wait_idle(self.idle_wait):
                self._say("waiting", "a daemon of this install claimed a run meanwhile")
                return False
            blocker = self._peer_blocker()
            if blocker:
                self._say("waiting", blocker)
                return False
            self._say("updating", f"installing nightly {target}")
            self._task = asyncio.ensure_future(self._install(target))
            try:
                outcome = await asyncio.shield(self._task)
            except asyncio.CancelledError:
                keep = True  # the install goes on: settle() waits for it, and the gate stays closed
                raise
            keep = outcome.ok
            return outcome.ok
        finally:
            if not keep:
                self.peers.open_gate()

    async def _install(self, target: str) -> Outcome:
        log.info("self-update: installing", extra={"nightly": target, "commit": self.install.commit})
        outcome = await self.installer.update(self.lookup)
        if outcome.ok:
            self.installed = True
            log.info(
                "self-update: installed; the daemon restarts on it",
                extra={"commit": outcome.commit, "old_commit": self.install.commit},
            )
            return outcome
        self.error, self.error_at, self._error_of_lookup = outcome.error, _now(), False
        self._retry[target] = self.clock() + self.install_retry
        self._say("waiting", f"the update to {target[:12]} failed; it is tried again later")
        log.error(
            "self-update: the update failed" + ("; the install may not start" if outcome.broken else ""),
            extra={"nightly": target, "error": outcome.error},
        )
        return outcome
