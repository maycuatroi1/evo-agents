"""The limits of the runs that map a project's entities to its code (``evo_agents.hub.server.kg_entity_map_run``)
and the shape of a run its routes answer with."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub import kg_entity_map_run as model

KG_ENTITY_MAP = "kg.entity_map"  # the audit action of an asked-for run and of its review run
MAX_LISTED = 10
ERROR_CHARS = 2000


class EntityMapRun(BaseModel):
    id: int
    project: str
    via: Literal["first_build", "stale", "manual"]
    state: Literal["queued", "running", "done", "failed"]
    why: list[str] = Field(description="what asked for the run")
    build_id: int | None
    agent_run_id: int | None = Field(description="the review run (lens kg_entity_map) of the agent that maps")
    maps: int = Field(description="the maps the review run posted")
    readable: dict | None = Field(description="what the run's sink read of the graph when it was queued, in counts")
    notes: list[str]
    error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


class EntityMapRuns(BaseModel):
    active: EntityMapRun | None
    runs: list[EntityMapRun]


def shown(row, project: str) -> EntityMapRun:
    body = model.shown(row.body if isinstance(row.body, dict) else {})
    return EntityMapRun(
        id=row.id,
        project=project,
        via=row.via,
        state=row.state,
        why=list(row.why or []),
        build_id=row.build_id,
        agent_run_id=row.agent_run_id,
        maps=row.maps,
        readable=body.get("readable"),
        notes=[str(n) for n in body.get("notes") or []],
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ended_at=row.ended_at,
    )
