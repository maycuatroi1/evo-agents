"""The values of the reads of API 1.3 added while the core plugins of evo-agents moved onto the API: a project that
enables the plugin, the read rule of a caller's labels, a project's join links, and who a run is for."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from evo_agents.hub.access import ProjectRules


@dataclass(frozen=True)
class EnabledProject:
    """A project that enables the plugin: its id (what the plugin's own rows keep), its name and the label an object
    gets when nothing names one (the harness source's, else the top of its ladder)."""

    id: int
    name: str
    default_label: Mapping[str, Any]


@dataclass(frozen=True)
class LabelReader:
    """The read rule of one caller in one project (from ``label_reader``): ``visible`` says whether they read an
    object with a label through a sink of the project, as the hub's routes check it."""

    project: str
    max_level: str | None  # the caller's grant's, as ``project_access`` gave it
    hub_sink: str | None  # the project's hub sink, which a read that names no sink goes through
    _rules: ProjectRules = field(repr=False, compare=False)

    def visible(self, label, sink: str | None = None) -> bool:
        """Whether an object carrying ``label`` (as stored) is read through ``sink``, else the project's hub sink;
        False when the project has none."""
        through = sink or self.hub_sink
        return through is not None and self._rules.visible(label, self.max_level, through)


@dataclass(frozen=True)
class InviteRef:
    id: int
    kind: str  # join, or register for the hub
    role: str | None
    for_login: str | None  # the GitHub login alone that may accept it, when it is bound to one
    created_at: datetime
    expires_at: datetime
    accepted_by: str | None  # the login of the member who accepted it
    accepted_at: datetime | None


@dataclass(frozen=True)
class RunActorRef:
    """Who and what a run is for: its project, the profile of the agent it runs, the member who dispatched it."""

    id: int
    project: str
    kind: str | None
    profile_id: int | None
    dispatcher_id: int | None
    dispatcher: str | None  # their login
