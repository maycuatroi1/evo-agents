"""The shapes and limits of the tool figures of runs (``evo_agents.hub.server.tool_stats``): what a run's calls came
to per tool, and the answers of its routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from evo_agents.hub import runs
from evo_agents.hub.digest import TOOL_NAME

TOOL_EVENTS = ("tool_call", "tool_call_update")
ENDED = ("completed", "failed")  # the statuses that end a call
NAMED_BY_TITLE = "claude-code"  # the runtime whose title of a call is its tool's name
UNNAMED = "unknown"
STATS_DAYS = 7
MAX_STATS_DAYS = 90


@dataclass
class ToolFigures:
    name: str
    calls: int = 0
    errors: int = 0
    duration_ms: int = 0
    skills: list[dict] = field(default_factory=list)  # the Skill tool's: per skill, its calls and errors


class ToolStat(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(alias=TOOL_NAME, description="the tool, as the module of tool_stats names it")
    calls: int
    errors: int = Field(description="calls an update of which said failed")
    duration_ms: int = Field(description="from each call to its result, added up; calls that never ended count none")


class SkillStat(BaseModel):
    skill: str = Field(description="the skill a call of the Skill tool loaded")
    calls: int
    errors: int = Field(description="calls an update of which said failed")


class RunToolStats(BaseModel):
    run_id: int
    runtime: str
    state: Literal[runs.RUN_STATES]  # pyright: ignore[reportInvalidTypeForm]
    finished_at: datetime | None
    tools: list[ToolStat] = Field(description="the most called first; empty for a run that has not ended")
    skills: list[SkillStat] = Field(
        default_factory=list, description="what the Skill tool loaded, the most called first; empty before schema 0087"
    )


class RuntimeSkillStat(SkillStat):
    runs: int = Field(description="runs that loaded the skill")


class RuntimeToolStat(ToolStat):
    runtime: str
    runs: int = Field(description="runs that called the tool")


class ProjectToolStats(BaseModel):
    project: str
    days: int
    first_day: date
    last_day: date
    runs: int = Field(description="runs that ended in those days, with tool calls or without")
    tools: list[RuntimeToolStat] = Field(description="per tool and runtime, the most called first")
    skills: list[RuntimeSkillStat] = Field(
        default_factory=list, description="per skill the Skill tool loaded, the most called first"
    )
