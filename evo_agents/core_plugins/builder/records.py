"""The kinds of proposals of @builder of a record of the project that has a code and revisions: a goal, a problem and an
opportunity (``goal_changes``, ``problem_changes``, ``opportunity_changes``), as ``RecordKind`` and ``EvidenceKind``.

A proposal names a record of the project (``target.<noun>``, its code) or none, for a new one whose code the hub picks
when it is proposed and keeps in the target. Its version is the record's revision, 0 for a new one. Approving writes
the fields the proposal sets over those of the record as its next revision, through the record's route service;
rolling back writes a copy of the revision before, or closes a record the proposal made. A problem and an opportunity
may carry lines of evidence too (``EvidenceKind``), written with the record when a writer approves and taken away
when it rolls back; a proposal of lines alone names an existing record and writes no revision.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.core_plugins.builder.kinds import (
    Change,
    ProposalKind,
    Target,
    Writing,
    copy_note,
    layer_problems,
    refuse,
    said_by_pydantic,
)
from evo_agents.hub.server.projects import ProjectAccess


class RecordKind(ProposalKind):
    """A kind of proposal of a record of ``model`` (its ``code``, ``FIELDS``, ``CLEARABLE`` and ``MAX_NOTE_CHARS``),
    written through route service ``routes`` (``found``, ``next_code``, ``fields_of``, ``check``, ``write``) as its
    ``write_model``."""

    noun: str  # goal: the key of its target and the word of its messages
    article = "a"  # of the noun: an opportunity
    model: ModuleType
    routes: ModuleType
    write_model: type[BaseModel]
    unchanged_refused = True  # a proposal that changes none of the fields of an existing record is refused
    dropped_note = "dropped"  # what rolling back a record the proposal made does to it, as its note says

    def fields_of(self, row: Any, access: ProjectAccess) -> dict:
        return self.routes.fields_of(row)

    def reopens(self, sets: dict) -> bool:
        """Whether ``sets`` opens the record again, so its close reason goes."""
        return sets.get("status") == self.model.OPEN

    def dropped(self, row: Any, access: ProjectAccess, proposal_id: int, applied: int) -> BaseModel:
        """The revision that closes record ``row``, which the proposal made, when it rolls back."""
        reason = f"rollback of proposal {proposal_id} of @builder"
        return self.routes.closing(row, self.model.DROPPED, reason, None, applied)

    async def prepare(self, conn: AsyncConnection, access: ProjectAccess) -> None:
        """What the hub does before it looks a record up."""

    def code(self, target: Target) -> str:
        return target.names[self.noun]

    def fields(self, target: Target) -> dict:
        assert target.record is not None  # located for a record
        return target.record

    async def locate(self, conn: AsyncConnection, access: ProjectAccess, change: Change, lock: bool) -> Target:
        """Record ``target.<noun>`` as it is now, or a new one under the next code when the target names none or that
        one."""
        await self.prepare(conn, access)
        ref = change.target.get(self.noun)
        row = await self.routes.found(conn, access.project_id, ref) if ref is not None else None
        if row is None:
            code = await self.routes.next_code(conn, access.project_id)
            if ref is not None and ref != code:
                raise ProposalError(
                    404,
                    f"project {access.name} has no {self.noun} {ref}; a new {self.noun} takes the code {code}",
                )
            title = f"a new {self.noun} {code} of project {access.name}"
            return Target(title, 0, {}, record={}, names={self.noun: code})
        code, fields = self.model.code(row.number), self.fields_of(row, access)
        held = {name: value for name, value in fields.items() if value is not None}
        title = f"{self.noun} {code} of project {access.name}"
        return Target(title, row.revision, held, record=fields, names={self.noun: code})

    def made(self, change: Change, target: Target) -> BaseModel:
        """The record ``change`` makes of the one ``target`` holds, as a write of its route; ProposalError (422) naming
        what is wrong with its shape."""
        noun = f"{self.article} {self.noun}"
        problems = [f"{name}: {noun} has no such field" for name in sorted(set(change.sets) - set(self.model.FIELDS))]
        problems += [f"{name}: {noun} always has one" for name in change.unset if name not in self.model.CLEARABLE]
        refuse(problems)
        fields = {**self.fields(target), **change.sets, **dict.fromkeys(change.unset)}
        if self.reopens(change.sets) and "close_reason" not in change.sets:
            fields["close_reason"] = None  # opened again
        try:
            return self.write_model.model_validate(fields)
        except ValidationError as exc:
            raise ProposalError(422, f"{said_by_pydantic(exc, self.noun)}; {NOTHING}") from None

    async def body(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> BaseModel:
        return self.made(change, target)

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        body = self.made(change, target)
        problems = layer_problems(change, target) if self.unchanged_refused and target.version else []
        try:
            return problems + await self.routes.check(conn, access, self.code(target), body)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, str(exc.detail)) from None

    async def write(self, w: Writing, body: Any, note: str, via: str) -> int:
        ref = self.code(w.target)
        body = body.model_copy(update={"note": note[: self.model.MAX_NOTE_CHARS], "if_revision": w.version})
        try:
            shown, written = await self.routes.write(w.conn, w.user, w.access, ref, body, via=via)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, str(exc.detail)) from None
        if not written:
            raise ProposalError(409, f"{self.noun} {ref} of project {w.access.name} is this already; {NOTHING}")
        return shown.revision

    async def restored(self, w: Writing, before: int) -> tuple[Any, str]:
        """A copy of revision ``before``; the revision that closes a record the proposal made when it is 0."""
        ref = self.code(w.target)
        row = await self.routes.found(w.conn, w.access.project_id, ref, before or None)
        if row is None:
            raise ProposalError(
                409, f"{self.noun} {ref} of project {w.access.name} has no revision {before}; {NOTHING}"
            )
        if before == 0:
            note = f"rollback of proposal {w.proposal_id} of @builder: the {self.noun} it made, {self.dropped_note}"
            return self.dropped(row, w.access, w.proposal_id, w.version), note
        return self.write_model.model_validate(self.fields_of(row, w.access)), copy_note(w, before)


class EvidenceKind(RecordKind):
    """A record that carries lines of evidence too, kept by ``lines`` (``wrong_lines``, ``add_lines``,
    ``remove_proposed``) as its ``line_model``, at most ``model.MAX_PROPOSED_EVIDENCE`` to a proposal."""

    lines: ModuleType
    line_model: type[BaseModel]

    def evidence_of(self, given: object) -> tuple[list[dict], list[str]]:
        """The lines of evidence a proposal carries as the hub keeps them, and what is wrong with them."""
        if not isinstance(given, list) or len(given) > self.model.MAX_PROPOSED_EVIDENCE:
            return [], [f"evidence is a list of at most {self.model.MAX_PROPOSED_EVIDENCE} lines"]
        found, wrong = [], []
        for at, item in enumerate(given, start=1):
            try:
                found.append(self.line_model.model_validate(item).model_dump())
            except ValidationError as exc:
                wrong.append(f"evidence {at}: {said_by_pydantic(exc, 'evidence')}")
        return found, wrong

    def _lines(self, change: Change) -> list:
        return [self.line_model.model_validate(line) for line in change.evidence]

    async def _held_id(self, w: Writing) -> int:
        """The id of the record of ``w.target``, which is there."""
        held = await self.routes.found(w.conn, w.access.project_id, self.code(w.target))
        assert held is not None  # located, or written by the proposal
        return held.id

    async def problems(
        self, conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
    ) -> list[str]:
        """What is wrong with the lines ``change`` carries as additions to the record (none yet for a new one), then
        with its fields, unless it carries lines alone."""
        wrong = []
        if change.evidence:
            held = await self.routes.found(conn, access.project_id, self.code(target)) if target.version else None
            wrong = await self.lines.wrong_lines(conn, None if held is None else held.id, self._lines(change))
        if change.evidence_only:
            return wrong
        return wrong + await super().problems(conn, access, change, target, hub_version)

    async def apply(self, w: Writing) -> int:
        """Write the record the change makes (unless it carries lines of evidence alone), then the lines it carries
        with the proposal's id; the record's revision."""
        if w.change.evidence_only:
            await self.routes.lock(w.conn, w.access.project_id)
            version = w.version
        else:
            version = await super().apply(w)
        if w.change.evidence:
            try:
                await self.lines.add_lines(
                    w.conn, w.user, w.access, await self._held_id(w), self._lines(w.change), proposal_id=w.proposal_id
                )
            except HTTPException as exc:
                raise ProposalError(exc.status_code, str(exc.detail)) from None
        return version

    async def roll_back(self, w: Writing) -> int:
        """The record's next revision, as ``RecordKind`` writes it, and the lines of evidence the proposal made taken
        away; a proposal of lines alone writes no revision."""
        if w.change.evidence_only:
            await self.routes.lock(w.conn, w.access.project_id)
            version = w.version
        else:
            version = await super().roll_back(w)
        await self.lines.remove_proposed(w.conn, w.user, w.access, await self._held_id(w), w.proposal_id)
        return version
