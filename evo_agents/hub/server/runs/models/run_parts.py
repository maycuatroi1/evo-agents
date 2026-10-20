"""The parts of a run its claim hands the worker: its caps, its repos, the plan's copy, its skills, its agent profile
and packs, and a plugin's runtime and environment. Leaf shapes of the models layer of ``evo_agents.hub.server.runs``,
which import nothing of the package; ``models`` imports them back, so callers import them from there as before."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class RunBudget(BaseModel):
    """The caps of a run the night shift queued (``evo_agents.hub.curator``)."""

    max_usd: float | None = Field(None, description="the most it may cost; Claude Code stops there")
    max_turns: int | None = Field(None, description="the most turns its agent may take")
    max_seconds: int | None = Field(None, description="the agent time it may use; a Codex run stops there")


class ClaimedBudget(RunBudget):
    """A run's caps as its worker gets them, with what it spent already when it goes on from a parked run."""

    spent_usd: float = Field(0.0, description="what the agent session it goes on in cost so far")
    spent_seconds: int = Field(0, description="the agent time it used so far")


class RunRepo(BaseModel):
    """A repo of a plan run, with the branch the plan names for it."""

    repo: str
    branch: str | None = Field(None, description="null when the plan names none: the branch checked out")


class PlanCopy(BaseModel):
    """A plan as the hub holds it at one revision."""

    revision: int
    body: dict


class RunSkill(BaseModel):
    """A skill the worker writes for the run's agent, as the hub holds it when the run is claimed: an author run's
    create-exec-plan, global, at its latest version."""

    name: str
    scope: Literal["global"] = "global"
    version: int
    sha256: str = Field(description="what the downloaded bytes must hash to")
    size: int
    url: str = Field(description="presigned GET of the bundle; a bearer credential until it expires")
    expires_at: datetime


class RunProfile(BaseModel):
    """The agent profile of a claimed run, at the version the run went under, for its worker to apply what the hub
    cannot (``evo_agents.hub.profiles``): tool, skill and model in the agent session, and the instructions."""

    handle: str
    version: int
    name: str
    instructions: str = Field(description="to add to the agent's system prompt; empty for none")
    permissions: dict = Field(
        description="the eight axes, each {mode: all} or {mode: allowlist, allow: [...]}; an axis a version from "
        "before the axis does not name allows all"
    )


class RunPack(BaseModel):
    """An agent pack the run's profile attaches (``evo_agents.plugins.agent_packs``), as the hub holds the version of
    its plugin the profile pins: the worker downloads the plugin's bundle, checks it, and builds the pack from its
    ``packs/<name>/`` into the run's directory for the agent's runtime."""

    pack: str = Field(description="<plugin>/<name>")
    plugin: str
    name: str
    version: str = Field(description="the version of the plugin the profile pins")
    sha256: str = Field(description="what the downloaded bundle must hash to")
    size: int
    url: str = Field(description="presigned GET of the plugin's bundle; a bearer credential until it expires")
    expires_at: datetime


class PluginRuntime(BaseModel):
    """A runtime a plugin enabled on a project declares (``evo_agents.plugins.runtimes``): the command of an ACP agent,
    at the plugin's version the hub has installed."""

    runtime: str = Field(description="<plugin>/<name>, as runs name it")
    plugin: str
    version: str = Field(description="the plugin's installed version")
    title: str
    command: list[str] = Field(
        description="the program, found on the worker's PATH unless absolute, then its arguments"
    )
    models: list[str] = Field(description="the models the agent offers, which the worker reports")
    secrets: list[str] = Field(
        description="the variables the agent needs, from the run's leases or the worker's environment"
    )


class RunMount(BaseModel):
    """A mount of a plugin's environment (``evo_agents.plugins.run_environments.Mount``)."""

    type: Literal["tmpfs", "volume"]
    target: str = Field(description="the absolute path in the container")
    name: str | None = Field(
        None, description="a volume's name within the plugin; the worker names it evo-<plugin>-<name>"
    )
    read_only: bool = False
    size_mb: int | None = Field(None, description="a tmpfs's size; null: the engine's default")


class RunEnvironment(BaseModel):
    """An environment a plugin enabled on a project declares (``evo_agents.plugins.run_environments``): a container
    image pinned by its digest, at the plugin's version the hub has installed."""

    environment: str = Field(description="<plugin>/<name>, as the environment axis of a profile names it")
    plugin: str
    version: str = Field(description="the plugin's installed version")
    title: str
    provider: Literal["container"] = "container"
    image: str = Field(description="[registry/]repository[:tag]@sha256:<digest>, pulled and run by that digest")
    network: Literal["none", "outbound"]
    mounts: list[RunMount] = Field(default_factory=list, description="besides the worktree and the run's home")
