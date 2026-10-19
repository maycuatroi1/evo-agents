"""The features of the hub that a hub admin turns on and off (criterion a11 of plan hub-agent-plugins), as core
declares them: each by its id, the layers it may be set on (the hub's own, ``instance``; a project's, ``project``),
the paths of the API it owns and the tools of the hub's /mcp it answers.

A feature is on unless a layer turns it off. A project's layer, when it sets a feature, wins over the hub's own, so a
hub admin may turn a feature off for the whole hub and back on for one project; a feature set for the whole hub only
(``instance`` alone among its layers) reads the hub's own layer alone. ``evo_agents.hub.server.hub_settings`` keeps
the layers as versioned settings and answers 404 to the routes and tools of a feature that is off; the web leaves it
out of its menus and answers 404 to its pages.

The monitor owns no route of its own (it reads the overview Home reads), so turning it off hides a page only.

Standard library only, besides the names of the knowledge graph's tools.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from evo_agents.kg.serve import TOOLS as KG_TOOLS

INSTANCE = "instance"
PROJECT = "project"
LAYERS = (INSTANCE, PROJECT)

_PROJECT = r"(?P<project>[a-z0-9][a-z0-9-]{0,99})"  # admin.PROJECT_NAME, as a group of a route's path


@dataclass(frozen=True)
class Feature:
    id: str
    layers: tuple[str, ...]  # where it may be set: the hub's own layer, and a project's when it owns project routes
    routes: tuple[re.Pattern, ...] = ()  # the API paths it owns, whole; a group ``project`` names the project's
    tools: frozenset[str] = field(default_factory=frozenset)  # the tools of the hub's /mcp it owns

    def per_project(self) -> bool:
        return PROJECT in self.layers


FEATURES = (
    Feature("chats", (INSTANCE,), (re.compile(r"/v1/chats(?:/.*)?"),)),
    Feature(
        "curator",
        LAYERS,
        (re.compile(rf"/v1/projects/{_PROJECT}/curator(?:/.*)?"),),
        frozenset(
            {
                "curator_figures",
                "curator_replay",
                "curator_findings",
                "curator_playbook",
                "history_sessions",
                "history_search",
                "history_clusters",
            }
        ),
    ),
    Feature(
        "kg",
        LAYERS,
        (re.compile(rf"/v1/kg/{_PROJECT}(?:/.*)?"), re.compile(rf"/v1/worker/kg/{_PROJECT}(?:/.*)?")),
        frozenset(tool["name"] for tool in KG_TOOLS),
    ),
    Feature("monitor", (INSTANCE,)),
)
BY_ID = {feature.id: feature for feature in FEATURES}
IDS = tuple(BY_ID)


def route_owner(path: str) -> tuple[Feature, str | None] | None:
    """The feature that owns API path ``path`` and the project the path names (None for a route of the whole hub);
    None when no feature owns it."""
    for feature in FEATURES:
        for route in feature.routes:
            found = route.fullmatch(path)
            if found is not None:
                return feature, found.groupdict().get("project")
    return None


def tool_owner(name: str) -> Feature | None:
    """The feature that owns tool ``name`` of the hub's /mcp; None when none does."""
    return next((feature for feature in FEATURES if name in feature.tools), None)


def enabled(feature: Feature, instance: Mapping, project: Mapping | None = None) -> bool:
    """Whether ``feature`` is on, given the values the hub's own layer sets and those of a project's layer (None
    outside a project): the project's when it sets the feature, else the hub's, else on."""
    if project is not None and feature.per_project() and feature.id in project:
        return bool(project[feature.id])
    return bool(instance.get(feature.id, True))


def problems(values: Mapping, layer: str) -> list[str]:
    """What is wrong with ``values`` as the fields one layer sets: a feature core does not declare, a value that is no
    boolean, a feature set for the whole hub only in a project's layer."""
    found = []
    for name, value in values.items():
        feature = BY_ID.get(name)
        if feature is None:
            found.append(f"{name}: not a feature of this hub, which has {', '.join(IDS)}")
        elif not isinstance(value, bool):
            found.append(f"{name}: true turns it on and false off, nothing else")
        elif layer not in feature.layers:
            found.append(f"{name}: it is turned on or off for the whole hub only, not for one project")
    return found
