"""The doctor reports of workers (plan credential-sharing, criterion a6): POST /v1/worker/doctor, and what a report
decides.

A daemon that says the feature ``runs.DOCTOR_REPORT`` runs ``evo-agents worker doctor`` when it starts and every 24
hours (``evo_agents.worker.doctor_report``) and sends its findings: for each, the code, the severity and the detail as
the doctor prints them, never a credential (the daemon redacts each detail, and the hub does again). The hub keeps the
newest report of each worker on its row (schema 0107): when it took it, how many findings are high and medium, and the
findings. ``GET /v1/org/workers`` and ``hub org worker list`` show the first three.

What a report decides:

- Narrowing a worker to a unit (``worker_shares``) is refused with 409 when its newest report is older than
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
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.redact import redact
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

NOTICE = "worker_doctor"  # runs.NOTICE_KINDS
MAX_FINDINGS = 100
MAX_DETAIL_CHARS = 2000
CODE_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class DoctorFinding(BaseModel):
    code: str = Field(pattern=CODE_PATTERN, description="what the doctor found, as `worker doctor --json` names it")
    severity: Literal["high", "medium", "low"]
    detail: str = Field(max_length=MAX_DETAIL_CHARS, description="what it found, as the doctor prints it")


class DoctorReport(BaseModel):
    findings: list[DoctorFinding] = Field(max_length=MAX_FINDINGS)


class DoctorAnswer(BaseModel):
    worker_id: int
    doctor_at: datetime = Field(description="when the hub took the report")
    doctor_high: int
    doctor_medium: int


def doctor_fresh(doctor_at):
    """Whether a report taken at ``doctor_at`` (a column) is fresh enough to narrow its worker, as a condition."""
    return doctor_at >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, runs.DOCTOR_FRESH_SECONDS)


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


async def _narrowed_and_shared(conn: AsyncConnection, worker_id: int) -> bool:
    ws, w = tables.worker_shares, tables.workers
    narrowed = exists().where(ws.c.worker_id == worker_id)
    query = select(w.c.shared & narrowed).where(w.c.id == worker_id)
    return bool((await conn.execute(query)).scalar_one())


async def _notify_owner(conn: AsyncConnection, row, findings: list[DoctorFinding]) -> None:
    codes = sorted({f.code for f in findings if f.severity == "high"})
    body = (
        f"The doctor of worker {row.name} found {', '.join(codes)} (high). It takes no run of the members of the units "
        "it is shared with until a report has no high finding; your own runs go on. Run `evo-agents worker doctor` on "
        "the machine to see how to fix each."
    )
    await notify(
        conn,
        user_id=row.owner_id,
        kind="notice",
        notice_kind=NOTICE,
        title=f"Worker {row.name}: its doctor found something high, so it stopped taking shared runs",
        body=body,
        details={"worker_id": row.id, "worker": row.name, "codes": codes},
        link=f"/workers/{row.id}",
    )


@worker_router.post("/doctor", response_model=DoctorAnswer, responses={403: {"model": ErrorBody}})
async def report_doctor(request: Request, report: DoctorReport, user: CurrentUser) -> DoctorAnswer:
    """Keep the newest doctor report of the worker whose token made the request; notify its owner when a report with a
    high finding stops it taking the runs of the units it is shared with."""
    w = tables.workers
    findings = [{**f.model_dump(), "detail": redact(f.detail)} for f in report.findings]
    high = sum(1 for f in report.findings if f.severity == "high")
    medium = sum(1 for f in report.findings if f.severity == "medium")
    async with request.app.state.engine.begin() as conn:
        query = select(w.c.id, w.c.owner_id, w.c.name, w.c.doctor_high, w.c.revoked_at).where(
            w.c.token_id == user.token_id
        )
        row = (await conn.execute(query.with_for_update())).one_or_none()
        if row is None or row.revoked_at is not None:
            raise HTTPException(403, "this worker token belongs to no live worker: join the machine again")
        taken = await conn.execute(
            update(w)
            .where(w.c.id == row.id)
            .values(doctor_at=func.now(), doctor_high=high, doctor_medium=medium, doctor_findings=findings)
            .returning(w.c.doctor_at)
        )
        doctor_at = taken.scalar_one()
        if high and not row.doctor_high and await _narrowed_and_shared(conn, row.id):
            await _notify_owner(conn, row, report.findings)
    log.info("doctor report", extra={"worker_id": row.id, "high": high, "medium": medium})
    return DoctorAnswer(worker_id=row.id, doctor_at=doctor_at, doctor_high=high, doctor_medium=medium)
