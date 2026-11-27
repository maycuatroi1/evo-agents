"""The doctor report of a worker (POST /v1/worker/doctor): its findings, the bounds a report is held to, the answer,
and the kind of the notice its owner gets."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

NOTICE = "worker_doctor"  # runs.NOTICE_KINDS
MAX_FINDINGS = 100
MAX_DETAIL_CHARS = 2000
CODE_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
NOT_LIVE = "this worker token belongs to no live worker: join the machine again"


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
