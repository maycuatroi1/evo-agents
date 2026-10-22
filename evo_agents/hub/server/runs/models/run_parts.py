"""The parts of a run its claim hands the worker: its caps, its repos, the plan's copy, its skills, its agent profile
and packs, a release run's cycle, and a plugin's runtime and environment. Leaf shapes of the models layer of
``evo_agents.hub.server.runs``, which import nothing of the package; ``models`` imports them back, so callers import
them from there as before."""

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


class RunOrigin(BaseModel):
    """The person at the origin of a claimed run, who dispatched it (for a chat run, the person whose message began
    the chain of agents): the worker names them as the co-author of the commits of a run its agent authors."""

    login: str = Field(description="the person's GitHub login")
    github_id: int | None = Field(
        description="the person's GitHub user id; null for a user the hub never saw on GitHub"
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


class ReleaseSpec(BaseModel):
    """What the worker of a release run is told (``evo_agents.hub.server.release_claims``): its cycle and mode, the
    repo and the commit its worktree starts at, and the release tags' prefix; it pushes release/<version> alone."""

    cycle_id: int
    mode: Literal["cut", "fix"] = Field(
        description="cut: a new version of the target; fix: the next patch of a red tag"
    )
    repo: str = Field(description="the repo of the release schedule")
    start_commit: str = Field(description="the commit the worktree starts at: a cut's target, a fix's red tag's")
    base_tag: str | None = Field(description="the newest release tag when the cycle opened")
    tag_prefix: str = Field(description="what a release tag starts with before its version: v of v*")
    red_tag: str | None = Field(None, description="the tag a fix run fixes forward; null for a cut")


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


class RunAutonomyClass(BaseModel):
    """One class of a run's autonomy snapshot (plan autonomy-levels): its mode, the source that set it and how."""

    mode: Literal["deny", "ask", "notify", "auto"]
    source: str = Field(description="the source that sets the mode, as GET /v1/autonomy names it")
    detail: str = Field(description="how that source sets it")
    condition: str | None = Field(None, description="the condition the mode acts under; null for none")


class RunAutonomy(BaseModel):
    """The autonomy a run acts under: its principal's effective autonomy as the hub took it when it queued the run,
    made stricter by every gate that found it lower since (``run_autonomy.recheck``)."""

    level: str = Field(description="the lowest level of the sources, A0 to A5")
    level_source: str = Field(description="the source that sets that level")
    classes: dict[str, RunAutonomyClass] = Field(description="K1 to K12, each its mode")
    environments: list[str] | None = Field(None, description="the environments it may run in; null: no limit")
    workers: list[str] | None = Field(None, description="the workers (names, lowered) that may take it; null: any")
    restricted: bool = Field(
        False, description="whether a class is below auto, so only a daemon that applies the modes takes it"
    )
    also: list[str] = Field(default_factory=list, description="logins besides the principal whose autonomy bounds it")
    taken_at: datetime = Field(description="when the hub took it")
    tightened_at: datetime | None = Field(None, description="when a gate last made it stricter; null: never")


class RunCard(BaseModel):
    """The autonomy card of a run (plan autonomy-levels, ``autonomy_card.card``): what its prompt opens with, built
    from the run's snapshot and its principal when the worker claims it."""

    card: str = Field(description="the card's text, first in the run's prompt")
    modes: dict[str, Literal["deny", "ask", "notify", "auto"]] = Field(
        description="K1 to K12, the mode each acts in for the agent: an ask a project policy sets shows as auto"
    )
    categories: list[str] = Field(description="the categories of decision its agent may ask")
    sources: dict[str, str] = Field(
        default_factory=dict, description="K1 to K12, what set the mode of each: a ceiling, a preference, a policy"
    )
    principal: str | None = Field(
        None, description="the login of the run's principal, which its commits name in the trailer Evo-Principal"
    )
