"""The shapes the web reads for autonomy (``evo_agents.hub.server.autonomy_page``, plan autonomy-levels, step 7): a
person's own settings page, the effective mode of each class of every seat of the org chart with the stops that hold,
and what a stop would cancel before it is written. No CLI prints them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evo_agents.hub.server.autonomy_models import Mode
from evo_agents.hub.server.org_models import CLASSES_FIELD, Level

FIGURE_DAYS = 30


class ClassFigures(BaseModel):
    """What the hub saw of one class over the last FIGURE_DAYS days for the person's runs."""

    refused: int = Field(description="the acts a gate or the worker's hook refused (autonomy.decided result=refused)")
    asked: int = Field(description="the acts a gate held for an answer (result=asked)")
    decisions: int = Field(description="the decisions asked in the class's categories")
    merged: int | None = Field(None, description="K5: the merges of the person's runs")
    reverted: int | None = Field(None, description="K5: those of them the hub reverted")
    rolled_back: int | None = Field(None, description="K6: those of them PROD was rolled back from")


class SettingsClass(BaseModel):
    name: str = Field(description="what the class covers, in words")
    mode: Mode = Field(description="the effective mode")
    source: str = Field(description="the source that sets it, as AutonomyClass.source")
    detail: str
    modes: list[Mode] = Field(description="the modes the class takes, the strictest first")
    ceiling: Mode = Field(description="the loosest mode the sources other than the preference allow")
    ceiling_source: str
    pref: Mode | None = Field(description="the mode the preference sets; null without a preference")
    lowered: bool = Field(description="the effective mode is stricter than the preference sets")
    figures: ClassFigures | None = Field(description="null for a class no gate records")


class HeldStop(BaseModel):
    id: int
    scope: Literal["login", "unit", "all"]
    target: str = Field(description="login:<login>, unit:<slug> or all")
    login: str | None
    unit: str | None = Field(description="the unit's slug, for a stop of a unit")
    reason: str | None
    stopped_at: datetime


class AutonomySettings(BaseModel):
    """The caller's own autonomy: the effective level and classes, the preference, the ceiling over it, the stop that
    holds them and the figures of each class."""

    principal: str
    level: Level
    level_source: str
    revision: int | None = Field(description="the preference's newest revision; null when none was set")
    pref_level: Level | None
    pref_classes: dict[str, str] = Field(description=CLASSES_FIELD)
    ceiling: Level = Field(description="the lowest level the sources other than the preference set")
    ceiling_source: str
    ceiling_detail: str = Field(description="what that source is, in words")
    classes: dict[str, SettingsClass]
    stop: HeldStop | None = Field(description="the stop that holds the caller; null when none does")
    days: int = FIGURE_DAYS


class SeatClass(BaseModel):
    mode: Mode
    source: str


class SeatClasses(BaseModel):
    id: int
    classes: dict[str, SeatClass] = Field(description="K1 to K12, the effective mode of the seat and its units")


class OrgAutonomy(BaseModel):
    """Every seat's effective mode of each class (its ceiling, its units' and the instance's) and the stops that
    hold."""

    seats: list[SeatClasses]
    stops: list[HeldStop]


class StopPreview(BaseModel):
    """What a stop of that scope would do now, once the caller may write it."""

    target: str
    runs: int = Field(description="the runs it would cancel or ask to stop")
    people: int | None = Field(description="the people it covers; null for the whole hub")
    holds: bool = Field(description="a stop of that scope holds already")
