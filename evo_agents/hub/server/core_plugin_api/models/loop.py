"""The values of the reads and helpers of API 1.3 for the audit, notices, goals, canvas claims, the Loop, agent types
and signals of a project, and their bounds."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

PLUGIN_NOTICE = "plugin_notice"  # the hub's notice kind of what a plugin tells a project's admins
ACTIVE = "active"
MAX_POINTS = 1000  # the most values one push takes


@dataclass(frozen=True)
class GoalMeasure:
    """An active goal of a project with a metric, and the value of its signal at a time: ``reached`` is None when
    nothing measured it."""

    code: str  # G-<number>
    statement: str
    metric: Mapping[str, Any]
    metric_text: str
    value: float | None
    target: float | None
    reached: bool | None


@dataclass(frozen=True)
class ClaimCardRef:
    """A claim of a project's canvas with its test card and the signal keys that measure it."""

    id: int
    number: int
    cell: str
    text: str
    test_card: Mapping[str, Any] | None
    signal_keys: tuple[str, ...]


@dataclass(frozen=True)
class WorkItemRef:
    """A work item of the plugin's on a project's Loop: ``key`` as the plugin's rules gave it, without the prefix the
    hub stores it with (``<plugin>:``)."""

    id: int
    kind: str
    key: str
    state: str
    created_at: datetime


@dataclass(frozen=True)
class ProjectAgentTypeRef:
    """An agent type of a plugin a project enables, as that project enabled it: ``enabled`` while it is not
    disabled and its profile is not deleted."""

    plugin: str
    name: str
    handle: str
    profile_id: int
    enabled: bool


@dataclass(frozen=True)
class SignalPoint:
    """One value of a signal for ``push_signals``, as ``push_signal`` takes it."""

    key: str
    value: float | None = None
    text: str | None = None
    measured_at: datetime | None = None
    tenant: str | None = None
    external_id: str | None = None


@dataclass(frozen=True)
class SignalsPushed:
    """What ``push_signals`` did with a batch: how many values it stored, how many it found stored already, and why
    it refused each of the others."""

    accepted: int
    duplicates: int
    rejected: tuple[str, ...]
