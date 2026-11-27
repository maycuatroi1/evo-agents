"""The shape of a project's draft graph branches (``evo_agents.hub.server.kg_branches``)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from evo_agents.kg import branch_ops


class BranchOut(BaseModel):
    id: int
    name: str
    status: str = Field(description="open, merged or closed")
    base_build_id: int | None = Field(description="the build of main it was opened at")
    created_by: int | None
    created_at: str
    ops: int = Field(description="operations applied to it")


class BranchesOut(BaseModel):
    branches: list[BranchOut]


class BranchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63, description="a-z, 0-9, '.', '_', '-'; not 'main'")


class BranchOps(BaseModel):
    ops: list[dict[str, Any]] = Field(
        min_length=1,
        max_length=branch_ops.MAX_OPS,
        description="operations, each with op: " + ", ".join(branch_ops.OPS),
    )


class BranchResult(BaseModel):
    """What a write, the diff or the check returned (``evo_agents.hub.kg_branches``)."""

    model_config = {"extra": "allow"}
