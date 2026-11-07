"""What a proposal of @builder changes (``evo_agents.core_plugins.builder.proposals``): the kinds of proposals, a
``Change`` and the ``Target`` it applies to, and the ``Kind`` each kind implements, which ``proposals.KINDS`` maps the
kinds to: where a change applies, how it is checked, applied and rolled back. A module ``*_changes`` holds the
``Kind`` of its kinds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.hub.server import hub_settings, plugin_configs
from evo_agents.hub.server.profile_store import Profile
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_settings import PROPOSAL_KINDS

(
    BRANDING,
    FEATURES,
    PLUGIN_CONFIG,
    AGENT,
    POLICY,
    PROBLEM,
    GOAL,
    OPPORTUNITY,
    PLUGIN,
    PLUGIN_INSTALL,
    PLUGIN_LISTING,
    SYSTEM,
) = PROPOSAL_KINDS


@dataclass(frozen=True)
class Change:
    """What a proposal changes: its kind, where (``target``), the fields it sets and those it unsets."""

    kind: str
    target: dict
    sets: dict
    unset: tuple[str, ...]
    summary: str = ""
    evidence: tuple[dict, ...] = ()  # lines of evidence of a problem, as ``ProblemEvidenceWrite`` dumps them

    def stored(self) -> dict:
        kept = {"set": self.sets, "unset": list(self.unset)}
        return kept | ({"evidence": list(self.evidence)} if self.evidence else {})

    @property
    def evidence_only(self) -> bool:
        """A proposal of a problem that carries lines of evidence and changes no field of the problem."""
        return self.kind in (PROBLEM, OPPORTUNITY) and bool(self.evidence) and not self.sets and not self.unset


@dataclass(frozen=True)
class Target:
    """Where a change applies, as it is now: how to name it, its version, and what each kind needs to write it."""

    title: str
    version: int
    held: dict = field(default_factory=dict)  # the values it sets now; a profile's fields for an agent
    settings: hub_settings.Place | None = None
    config: plugin_configs.Place | None = None
    schema: dict | None = None
    profile: Profile | None = None
    policy: dict | None = None  # the body of the policy's revision now, for a policy
    goal: dict | None = None  # the fields of the goal now as its route names them, {} for a new one, for a goal
    problem: dict | None = None  # the same of the problem now, for a problem
    opportunity: dict | None = None  # the same of the opportunity now, for an opportunity
    names: dict = field(default_factory=dict)  # what the hub adds to the target it keeps: a goal's or problem's code


@dataclass(frozen=True)
class Writing:
    """One write of a change of proposal ``proposal_id`` by ``user``: approving it while its target is at ``version``
    (the proposal's base version), or rolling it back while its target is at ``version`` (the one approving made)."""

    conn: AsyncConnection
    user: Principal
    access: ProjectAccess
    change: Change
    target: Target
    version: int
    hub_version: str
    proposal_id: int

    @property
    def note(self) -> str:
        """The note of a revision approving writes."""
        return f"proposal {self.proposal_id} of @builder: {self.change.summary}"


@dataclass(frozen=True)
class Kind:
    """What a kind of proposal does: ``locate`` where a change applies as it is now (``lock``: for a write), ``check``
    it as the next version of its target (ProposalError naming what is wrong), ``apply`` it and ``roll_back`` an
    applied one, each giving the version it wrote."""

    locate: Callable[[AsyncConnection, ProjectAccess, Change, bool], Awaitable[Target]]
    check: Callable[[AsyncConnection, ProjectAccess, Change, Target, str], Awaitable[None]]
    apply: Callable[[Writing], Awaitable[int]]
    roll_back: Callable[[Writing], Awaitable[int]]


def refuse(problems: list[str]) -> None:
    """ProposalError (422) naming ``problems``, when there are any."""
    if problems:
        raise ProposalError(422, f"{'; '.join(problems)}; {NOTHING}")


def layer_problems(change: Change, target: Target, values: dict | None = None) -> list[str]:
    """Each field ``change`` unsets that the layer does not set, or that it changes nothing; ``values``: what it sets,
    when not ``change.sets``."""
    problems = [
        f"{name}: the layer does not set it, so there is nothing to unset"
        for name in change.unset
        if name not in target.held
    ]
    sets = change.sets if values is None else values
    same = all(target.held.get(name) == value for name, value in sets.items())
    if not problems and not change.unset and same:
        problems.append(f"{target.title} has these values already: it changes nothing")
    return problems


def said_by_pydantic(exc: ValidationError, what: str = "goal") -> str:
    return "; ".join(f"{'.'.join(map(str, error['loc'])) or what}: {error['msg']}" for error in exc.errors())
