"""Whom a worker is shared with (GET /v1/org/workers and the writes under it): the shape each answer shows."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

NO_WORKER = "you have no worker {id} on this hub: see GET /v1/org/workers"


class WorkerShares(BaseModel):
    """Whom a worker is shared with: its owner's writers, narrowed to ``units`` when it names any."""

    id: int
    worker: str = Field(description="the worker's name, unique among its owner's live workers")
    owner: str = Field(description="the login of the member who registered it")
    shared: bool = Field(description="whether other members' runs may go to it")
    units: list[str] = Field(
        description="the slugs of the units it is narrowed to, sorted; none: shared with every writer of a project "
        "it serves"
    )
    doctor_at: datetime | None = Field(None, description="when its daemon sent the newest doctor report; null: none")
    doctor_high: int | None = Field(None, description="findings of severity high in that report; null: no report")
    doctor_medium: int | None = Field(None, description="findings of severity medium in that report; null: no report")
