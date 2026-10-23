"""The daemon's doctor report (feature ``runs.DOCTOR_REPORT``; plan credential-sharing, criterion a6).

When it starts and every ``runs.DOCTOR_EVERY_SECONDS`` (24 hours) the daemon runs the checks of ``evo-agents worker
doctor`` (``doctor.examine``, the same as ``worker doctor --json``) on its machine, in a thread, and sends the findings
to ``POST /v1/worker/doctor``: for each, the code, the severity and the detail the doctor prints, the detail redacted
(``evo_agents.hub.redact``, the worker's token among the values) so a credential never leaves the machine. Neither the
subject, the fix, the admins nor the PATH goes. The hub keeps the newest report; a worker narrowed to units of the org
chart takes their members' runs only while it has no high finding (``evo_agents.hub.server.worker_doctor``).

A hub of an older release answers 404: the daemon logs it once at info and tries again at the next turn. Any other
failure is a warning, and the loop goes on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping

from evo_agents.hub import runs
from evo_agents.hub.redact import redact
from evo_agents.worker import doctor
from evo_agents.worker.hubapi import HubProblem, Refused, WorkerHub

log = logging.getLogger(__name__)


def examine_here(env: Mapping[str, str]) -> doctor.Report:
    """The doctor's report of this machine, as the daemon's environment ``env`` sees it."""
    return doctor.examine(doctor.Machine.here(env=env))


def payload(report: doctor.Report, secrets: tuple[str | None, ...] = ()) -> dict:
    """What the hub gets of ``report``: code, severity and redacted detail of each finding, in the doctor's order."""
    return {
        "findings": [
            {"code": f.code, "severity": f.severity, "detail": redact(f.detail, secrets)} for f in report.ordered()
        ]
    }


async def send_once(
    hub: WorkerHub, examine: Callable[[], doctor.Report], secrets: tuple[str | None, ...] = ()
) -> dict | None:
    """Examine the machine and send the report; the hub's answer, or None when it could not be sent."""
    try:
        report = await asyncio.to_thread(examine)
    except Exception:  # a bug of a check must not end the daemon's loop
        log.exception("the doctor could not examine the machine")
        return None
    body = payload(report, secrets)
    counts = report.counts()
    try:
        answer = await hub.doctor(body)
    except Refused as exc:
        level = logging.INFO if exc.status == 404 else logging.WARNING
        log.log(level, "the hub took no doctor report", extra={"status": exc.status, "error": str(exc)})
        return None
    except HubProblem as exc:
        log.warning("doctor report not sent", extra={"status": exc.status, "error": str(exc)})
        return None
    log.info("doctor report sent", extra={"high": counts[doctor.HIGH], "medium": counts[doctor.MEDIUM]})
    return answer


async def report_loop(
    hub: WorkerHub,
    stopping: asyncio.Event,
    examine: Callable[[], doctor.Report],
    *,
    secrets: tuple[str | None, ...] = (),
    every: float = runs.DOCTOR_EVERY_SECONDS,
) -> None:
    """Send a report now, then every ``every`` seconds, until ``stopping`` is set."""
    while not stopping.is_set():
        await send_once(hub, examine, secrets)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stopping.wait(), every)
