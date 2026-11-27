"""What a proposal of @builder changes (``evo_agents.core_plugins.builder.proposals``): the kinds of proposals, a
``Change`` and the ``Target`` it applies to, and the ``ProposalKind`` each kind is a subclass of, which
``proposals.KINDS`` maps the kinds to: where a change applies, how it is checked, applied and rolled back. A module
``*_changes`` holds the kinds of its proposals; ``records`` the base of a goal's, a problem's and an opportunity's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.plugins.models.configs import Place
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
    config: Place | None = None
    schema: dict | None = None
    profile: Profile | None = None
    policy: dict | None = None  # the body of the policy's revision now, for a policy
    # the fields of the goal, problem or opportunity now as its route names them, {} for a new one
    record: dict | None = None
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


class ProposalKind:
    """What a kind of proposal does, as a Template Method: ``locate`` where a change applies as it is now (``lock``:
    for a write), ``check`` it as the next version of its target (ProposalError naming what is wrong), ``apply`` it and
    ``roll_back`` an applied one, each giving the version it wrote.

    ``check`` refuses the ``problems`` a kind finds; ``apply`` writes the ``body`` the change makes of its target, and
    ``roll_back`` writes the body version ``applied - 1`` had again (``restored``), each through the kind's ``write``.
    A kind fills in ``locate``, ``write`` and the hooks it needs, and overrides a step whose shape is its own."""

    async def locate(self, conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
        raise NotImplementedError

    async def body(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> Any:
        """What ``change`` makes of ``target``, as ``write`` takes it; ProposalError naming what is wrong with it."""
        return change.sets

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        """Each thing wrong with ``change`` as the next version of ``target``."""
        return []

    async def check(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> None:
        refuse(await self.problems(conn, access, change, target, hub_version))

    async def write(self, w: Writing, body: Any, note: str, via: str) -> int:
        """Write ``body`` as the next version of the target of ``w`` while it is at ``w.version``; that version."""
        raise NotImplementedError

    async def apply(self, w: Writing) -> int:
        body = await self.body(w.conn, w.access, w.change, w.target, w.hub_version)
        return await self.write(w, body, w.note, f" proposal={w.proposal_id}")

    async def restored(self, w: Writing, before: int) -> tuple[Any, str]:
        """The body version ``before`` of the target had, and the note of the revision writing it again;
        ProposalError (409) when there is none."""
        raise NotImplementedError

    async def roll_back(self, w: Writing) -> int:
        """The target's next version, a copy of version ``applied - 1``."""
        before = w.version - 1
        body, note = await self.restored(w, before)
        return await self.write(w, body, note, f" proposal={w.proposal_id} copy-of={before}")


def copy_note(w: Writing, before: int) -> str:
    """The note of a revision rolling the proposal of ``w`` back to a copy of revision ``before``."""
    return f"rollback of proposal {w.proposal_id} of @builder: a copy of revision {before}"


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
