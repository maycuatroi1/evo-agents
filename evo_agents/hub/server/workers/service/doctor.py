"""The doctor reports of workers (plan credential-sharing, criterion a6): POST /v1/worker/doctor, and what a report
decides.

A daemon that says the feature ``runs.DOCTOR_REPORT`` runs ``evo-agents worker doctor`` when it starts and every 24
hours (``evo_agents.worker.doctor_report``) and sends its findings: for each, the code, the severity and the detail as
the doctor prints them, never a credential (the daemon redacts each detail, and the hub does again). The hub keeps the
newest report of each worker on its row (schema 0108): when it took it, how many findings are high and medium, and the
findings. ``GET /v1/org/workers`` and ``hub org worker list`` show the first three.

What a report decides:

- Narrowing a worker to a unit (``shares``) is refused with 409 when its newest report is older than
  ``runs.DOCTOR_FRESH_SECONDS`` (48 hours), when there is none (a daemon of an older release sends none), or when it
  has a high finding (``share_refusal``).
- A worker narrowed to units takes the runs of their members only while its newest report has no high finding
  (``worker_fit.unit_admits``); its owner's runs, and those of every writer on a shared worker with no unit, do not
  depend on it.
- A report with a high finding on a shared worker narrowed to units, whose report before had none, sends its owner the
  notice ``worker_doctor``, which names the codes of the high findings.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs
from evo_agents.hub.redact import redact
from evo_agents.hub.server.base.errors import Forbidden
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.workers.models.doctor import NOT_LIVE, NOTICE, DoctorAnswer, DoctorFinding, DoctorReport
from evo_agents.hub.server.workers.repository import doctor as store

log = logging.getLogger(__name__)


def share_refusal(doctor_at: datetime | None, doctor_high: int | None, fresh: bool | None) -> str | None:
    """Why a worker whose newest doctor report is (``doctor_at``, ``doctor_high``, ``doctor_fresh``) may not be
    narrowed to a unit, or None when it may."""
    hours = runs.DOCTOR_FRESH_SECONDS // 3600
    if doctor_at is None:
        return (
            "its daemon has sent no doctor report: update it to a release that sends one (feature doctor_report), "
            "then share it"
        )
    if not fresh:
        return f"its newest doctor report is from {doctor_at.isoformat()}, more than {hours} hours ago"
    if doctor_high:
        return (
            f"its newest doctor report has {doctor_high} high finding(s): fix them (`evo-agents worker doctor` on the "
            "machine says how), then wait for the next report or restart the daemon"
        )
    return None


async def notify_owner(conn: AsyncConnection, row, findings: list[DoctorFinding]) -> None:
    codes = sorted({f.code for f in findings if f.severity == "high"})
    body = (
        f"The doctor of worker {row.name} found {', '.join(codes)} (high). It takes no run of the members of the units "
        "it is shared with until a report has no high finding; your own runs go on. Run `evo-agents worker doctor` on "
        "the machine to see how to fix each."
    )
    await NOTIFIER.notify(
        conn,
        user_id=row.owner_id,
        kind="notice",
        notice_kind=NOTICE,
        title=f"Worker {row.name}: its doctor found something high, so it stopped taking shared runs",
        body=body,
        details={"worker_id": row.id, "worker": row.name, "codes": codes},
        link=f"/workers/{row.id}",
    )


async def keep(conn: AsyncConnection, user: Principal, report: DoctorReport) -> DoctorAnswer:
    """Keep ``report`` as the newest of the worker whose token ``user`` holds, each detail redacted; 403 for a token
    of no live worker. Notify its owner when a high finding stops it taking the runs of the units it is shared with."""
    findings = [{**f.model_dump(), "detail": redact(f.detail)} for f in report.findings]
    high = sum(1 for f in report.findings if f.severity == "high")
    medium = sum(1 for f in report.findings if f.severity == "medium")
    row = await store.worker_of_token(conn, user.token_id)
    if row is None or row.revoked_at is not None:
        raise Forbidden(NOT_LIVE)
    doctor_at = await store.keep_report(conn, row.id, high, medium, findings)
    if high and not row.doctor_high and await store.narrowed_and_shared(conn, row.id):
        await notify_owner(conn, row, report.findings)
    log.info("doctor report", extra={"worker_id": row.id, "high": high, "medium": medium})
    return DoctorAnswer(worker_id=row.id, doctor_at=doctor_at, doctor_high=high, doctor_medium=medium)
