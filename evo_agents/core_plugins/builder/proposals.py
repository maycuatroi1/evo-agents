"""The proposals of @builder (``evo_agents.core_plugins.builder``): checking one, keeping it, deciding it, applying
it and rolling it back. Table builder_proposals, schema 0043.

A proposal is a change of one setting the hub versions, as the hub's own routes make it:

- ``branding``: the hub's name and colours (``evo_agents.hub.branding``); its logo and favicon are uploads, which a
  proposal may unset and never set. A hub admin decides it.
- ``features``: the hub's own layer of features or the project's (``target.layer``, ``evo_agents.hub.features``).
  A hub admin decides it, as the hub's routes of features ask.
- ``plugin_config``: the project's layer of the config of a plugin the project enabled, or the layer of one of its
  agent profiles (``target.plugin``, ``target.agent``). A secret field is never set: the agent would see its value.
  An admin of the project decides it.
- ``agent``: the name, description, instructions and agent packs of an agent profile of the project
  (``target.agent``), not the default one. An admin of the project decides it.
- ``policy``: the project's autonomy policy (``evo_agents.hub.autonomy``), its keys named by their dotted path
  (``merge.hold_until``, ``repos.<repo>.mode``); its version is the policy's revision. An admin of the project
  decides it, and the hub writes the next revision as the policy's route does (``autonomy_policies.write``).
- ``problem``: a problem of the project (``evo_agents.hub.problems``), new or one that exists (``target.problem``,
  its code), its fields named as the problem's route names them (statement, who, when, workaround, metric, rank,
  owner, status, close_reason); its version is the problem's revision, 0 for a new one, whose code the hub picks when
  it is proposed and keeps in the target. A writer of the project decides it, and the hub writes the next revision as
  the problem's route does (``problems.write``); rolling back a new problem drops it. It may carry ``evidence`` (plan
  problem-intake, criterion a1): lines of sources that show the problem hurts, each as the evidence route takes one,
  which the hub makes with the problem when a writer approves; a proposal of lines alone names an existing problem,
  sets nothing, and writes no revision. Rolling back takes the lines it made away.
- ``opportunity``: an opportunity of the project (``evo_agents.hub.opportunities``), new or one that exists
  (``target.opportunity``, its code), its fields named as the opportunity's route names them (statement, beneficiary,
  leverage, window_until, window_note, answers, metric, rank, owner, status, close_reason); its version is the
  opportunity's revision, 0 for a new one. A writer of the project decides it, and the hub writes the next revision as
  the opportunity's route does; rolling back a new one drops it. It may carry ``evidence`` as a problem does (plan
  opportunity-intake, criterion a2), with the seven signals of an opportunity too; the logic is
  ``builder.opportunity_changes``.
- ``goal``: a goal of the project (``evo_agents.hub.goals``), new or one that exists (``target.goal``, its code or
  alias), its fields named as the goal's route names them (statement, metric, deadline, canvas, problem, rank, owner,
  status, close_reason); its version is the goal's revision, 0 for a new one, whose code the hub picks when it is
  proposed and keeps in the target. An admin of the project decides it, and the hub writes the next revision as the
  goal's route does (``goals.write``); rolling back a new goal closes it (dropped).
- ``system``: an agent system of the project (``evo_agents.hub.system_spec``, plan multi-agent-systems, criterion
  a11), new or one that exists (``target.system``, its slug): the spec it would have, whole, and the agent profiles
  and named graphs it needs that the project lacks; its version is the system's revision, 0 for a new one. An admin
  of the project decides it; approving makes the profiles and graphs, then writes the spec as the next revision.
  Rolling back writes the revision before again, or deletes a new system. The logic is ``builder.system_changes``.
- ``plugin``, ``plugin_install``, ``plugin_listing``: an action, not a setting (plan plugin-market, criterion a8): a
  plugin a member asked @builder to make for an industry, installing its release on the hub (a hub admin decides it),
  and listing it on a market. Approving one does it once, as version 1, and it never rolls back: a plugin or a listing
  hands @builder its task in the proposal's thread for the member who approved it, an install installs the release.
  The logic is ``builder.plugin_changes``.

What each kind does (where its change applies, how it is checked, applied and rolled back) is its ``kinds.Kind``,
which ``KINDS`` maps it to: ``setting_changes`` for branding, features, plugin_config, agent and policy,
``problem_changes``, ``goal_changes``, ``opportunity_changes``, ``system_changes`` and ``plugin_changes`` for the
others.

Proposing checks the change against the setting as it is now, as its route would, and keeps the setting's version
(``base_version``): nothing is written to the setting. Approving applies the change as the setting's next version
only while the setting is still at that version, through the hub's own functions (the audit row of the change
included), in a savepoint: a change the hub refuses then marks the proposal failed with why. Rolling back an applied
proposal, while the setting is still at the version it made, makes the setting's next version a copy of the one
before it. Each decision adds an audit row ``builder.decide``; each proposal ``builder.propose``.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import ARRAY, Text, any_, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder import (
    MAX_FIELDS,
    MAX_SUMMARY_CHARS,
    goal_changes,
    opportunity_changes,
    plugin_changes,
    problem_changes,
    setting_changes,
    system_changes,
)
from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.core_plugins.builder.kinds import (
    AGENT,
    BRANDING,
    FEATURES,
    GOAL,
    OPPORTUNITY,
    PLUGIN,
    PLUGIN_CONFIG,
    PLUGIN_INSTALL,
    PLUGIN_LISTING,
    POLICY,
    PROBLEM,
    SYSTEM,
    Change,
    Kind,
    Target,
    Writing,
)
from evo_agents.hub import chats, goals, system_spec, tables
from evo_agents.hub import problems as problem_model
from evo_agents.hub.access import has_role
from evo_agents.hub.self_managed import SelfManaged, state_of
from evo_agents.hub.server import audit
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal, admin_login
from evo_agents.hub.tables_settings import PROPOSAL_KINDS, PROPOSAL_STATES

log = logging.getLogger(__name__)

PENDING, APPLIED, REJECTED, FAILED, ROLLED_BACK = PROPOSAL_STATES
HUB_WIDE = (BRANDING, FEATURES, PLUGIN_INSTALL)  # a hub admin decides them; an admin of the project the others
ACTIONS = plugin_changes.ACTIONS  # done once when approved, never rolled back
BY_WRITERS = (PROBLEM, OPPORTUNITY)  # but these, which a writer of the project decides
APPROVE, REJECT, ROLLBACK = "approve", "reject", "rollback"
LAYERS = ("hub", "project")  # target.layer of a proposal of features
TARGET_KEYS = {  # kind -> (the keys its target needs, those it may have)
    BRANDING: ((), ()),
    FEATURES: (("layer",), ()),
    PLUGIN_CONFIG: (("plugin",), ("agent",)),
    AGENT: (("agent",), ()),
    POLICY: ((), ()),
    PROBLEM: ((), ("problem",)),
    GOAL: ((), ("goal",)),
    OPPORTUNITY: ((), ("opportunity",)),
    PLUGIN: ((), ()),
    PLUGIN_INSTALL: ((), ()),
    PLUGIN_LISTING: ((), ()),
    SYSTEM: (("system",), ()),
}
MAX_ERROR_CHARS = 2000
MAX_SHOWN_BYTES = 3000  # of the values a proposal sets, in its message in the thread
NOTICE = "builder_proposal"  # evo_agents.hub.runs.NOTICE_KINDS
AUDIT_PROPOSE = "builder.propose"  # the agent of a run proposed a change, for the run's dispatcher
AUDIT_DECIDE = "builder.decide"  # an admin approved, rejected or rolled back a proposal


class BuilderProposal(BaseModel):
    """A change @builder proposed, and what became of it."""

    id: int
    project: str
    kind: Literal[PROPOSAL_KINDS]
    target: dict[str, str] = Field(
        description="layer (features), plugin and agent (plugin_config), agent (agent), problem (problem: its code, "
        "which the hub picks for a new one), goal (goal: its code, which the hub picks for a new one), system (system: its slug); "
        "nothing for policy and the actions plugin, plugin_install and plugin_listing, whose fields are in change.set"
    )
    change: dict[str, Any] = Field(
        description="set: field -> value; unset: the fields it clears; a key of the policy by its dotted path"
    )
    summary: str
    base_version: int = Field(description="the setting's version it was proposed on, which approving needs still")
    state: Literal[PROPOSAL_STATES]
    approver: Literal["hub_admin", "project_writer", "project_admin"] = Field(description="who may decide it")
    proposed_by: str = Field(
        description="the member the run proposed for: the admin who asked @builder, or the owner of the Curator's night "
        "shift for a proposal of its review run"
    )
    proposed_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    applied_version: int | None = Field(description="the setting's version approving made")
    rollback_version: int | None = Field(description="the setting's version rolling back made")
    error: str | None = Field(description="why the hub could not apply it, for a failed one")
    run_id: int | None = Field(description="the chat run of @builder, or the Curator's review run, it came from")
    opened_run_id: int | None = Field(
        None, description="an approved plugin or plugin_listing: the chat run of @builder that took its task"
    )
    outcome: str | None = Field(None, description="an approved action: what approving it did")
    link: str | None = Field(
        description="the hub's message that shows it in its thread; none for a review run's or an intake's"
    )


# Reading


def _select():
    b, p, u = tables.builder_proposals, tables.projects, tables.users
    proposer, decider = u.alias("proposer"), u.alias("decider")
    return (
        select(
            b,
            p.c.name.label("project"),
            p.c.self_managed,
            p.c.trusted_owners,
            proposer.c.login.label("proposer"),
            decider.c.login.label("decider"),
        )
        .join_from(b, p, p.c.id == b.c.project_id)
        .join(proposer, proposer.c.id == b.c.proposed_by)
        .outerjoin(decider, decider.c.id == b.c.decided_by)
    )


def approver_of(kind: str, own: bool = False) -> str:
    """Who may decide a proposal of ``kind``: a hub admin, a writer of the project or an admin of it. ``own``: the
    install of a repo of an owner its self-managed project trusts, which an admin of the project decides."""
    if own:
        return "project_admin"
    return "hub_admin" if kind in HUB_WIDE else "project_writer" if kind in BY_WRITERS else "project_admin"


def is_own(kind: str, sets: Mapping, state: SelfManaged) -> bool:
    """Whether a proposal of ``kind`` setting ``sets`` is the install of a repo ``state`` trusts: its project's admins
    decide it, and the plugin is the project's alone."""
    return kind == PLUGIN_INSTALL and state.trusts_repo(sets.get("repo"))


async def own_install(conn: AsyncConnection, access: ProjectAccess, kind: str, sets: Mapping) -> bool:
    """``is_own`` for the project of ``access``."""
    return kind == PLUGIN_INSTALL and is_own(kind, sets, await state_of(conn, access.project_id))


def view(row) -> BuilderProposal:
    link = chats.chat_link(row.chat_id, row.message_id) if row.chat_id is not None else None
    return BuilderProposal(
        id=row.id,
        project=row.project,
        kind=row.kind,
        target=row.target,
        change=row.change,
        summary=row.summary,
        base_version=row.base_version,
        state=row.state,
        approver=approver_of(
            row.kind,
            is_own(row.kind, row.change.get("set", {}), SelfManaged(row.self_managed, tuple(row.trusted_owners))),
        ),
        proposed_by=row.proposer,
        proposed_at=row.proposed_at,
        decided_by=row.decider,
        decided_at=row.decided_at,
        applied_version=row.applied_version,
        rollback_version=row.rollback_version,
        error=row.error,
        run_id=row.run_id,
        opened_run_id=row.opened_run_id,
        outcome=row.outcome,
        link=link,
    )


async def listed(
    conn: AsyncConnection, project_id: int, state: str | None = None, limit: int = 100, kinds: list[str] | None = None
) -> list[BuilderProposal]:
    """The proposals of the project, the newest first; those in ``state``, and of ``kinds``, alone when given."""
    b = tables.builder_proposals
    query = _select().where(b.c.project_id == project_id)
    if state is not None:
        query = query.where(b.c.state == state)
    if kinds is not None:
        query = query.where(b.c.kind.in_(kinds))
    return [view(row) for row in await conn.execute(query.order_by(b.c.id.desc()).limit(limit))]


async def shown(conn: AsyncConnection, access: ProjectAccess, proposal_id: int) -> BuilderProposal:
    b = tables.builder_proposals
    row = (await conn.execute(_select().where(b.c.id == proposal_id, b.c.project_id == access.project_id))).first()
    if row is None:
        raise ProposalError(404, f"project {access.name} has no proposal {proposal_id} of @builder")
    return view(row)


async def in_thread(conn: AsyncConnection, project_id: int, proposal_id: int, chat_id: int, root_id: int):
    """The row of proposal ``proposal_id`` of the project when it was made in thread ``root_id`` of chat ``chat_id``."""
    b = tables.builder_proposals
    query = select(b).where(
        b.c.id == proposal_id, b.c.project_id == project_id, b.c.chat_id == chat_id, b.c.thread_root_id == root_id
    )
    return (await conn.execute(query)).first()


async def _locked(conn: AsyncConnection, access: ProjectAccess, proposal_id: int):
    b = tables.builder_proposals
    query = select(b).where(b.c.id == proposal_id, b.c.project_id == access.project_id).with_for_update(of=b)
    row = (await conn.execute(query)).first()
    if row is None:
        raise ProposalError(404, f"project {access.name} has no proposal {proposal_id} of @builder")
    return row


def change_of_row(row) -> Change:
    evidence = tuple(row.change.get("evidence") or ())
    return Change(
        row.kind, dict(row.target), dict(row.change["set"]), tuple(row.change["unset"]), row.summary, evidence
    )


# What the agent proposes


def _field_problems(sets: object, unset: object) -> list[str]:
    if not isinstance(sets, dict) or not isinstance(unset, list) or not all(isinstance(n, str) for n in unset):
        return ["set is an object of field -> value and unset a list of field names"]
    problems = []
    if not sets and not unset:
        problems.append("the change sets and unsets no field")
    if len(sets) + len(unset) > MAX_FIELDS:
        problems.append(f"a proposal sets and unsets at most {MAX_FIELDS} fields")
    both = sorted(set(sets) & set(unset))
    if both:
        problems.append(f"{', '.join(both)}: both set and unset")
    if len(set(unset)) != len(unset):
        problems.append("unset names a field more than once")
    return problems


def _evidence_of(kind: str, given: object) -> tuple[list[dict], list[str]]:
    """The lines of evidence a proposal of ``kind`` carries as the hub keeps them, and what is wrong with them."""
    if given is None or given == []:
        return [], []
    if kind not in (PROBLEM, OPPORTUNITY):
        return [], ["evidence belongs to a problem or opportunity proposal"]
    return opportunity_changes.evidence_of(given) if kind == OPPORTUNITY else problem_changes.evidence_of(given)


def change_of(arguments: Mapping) -> Change:
    """The change the tool's ``arguments`` propose, its shape checked; ProposalError (422) saying what is wrong."""
    kind = arguments.get("kind")
    if kind not in PROPOSAL_KINDS:
        raise ProposalError(422, f"kind is one of {', '.join(PROPOSAL_KINDS)}")
    summary = " ".join(str(arguments.get("summary") or "").split())
    target, sets, unset = arguments.get("target") or {}, arguments.get("set") or {}, arguments.get("unset") or []
    problems = [] if 0 < len(summary) <= MAX_SUMMARY_CHARS else [f"summary: 1 to {MAX_SUMMARY_CHARS} characters"]
    needed, allowed = TARGET_KEYS[kind]
    if not isinstance(target, dict) or not all(isinstance(value, str) and value for value in target.values()):
        problems.append("target maps its keys to names")
        target = {}
    missing = [key for key in needed if key not in target]
    extra = sorted(set(target) - set(needed) - set(allowed))
    if missing or extra:
        named = ", ".join(needed + allowed) or "nothing"
        problems.append(f"the target of a {kind} proposal names {named}")
    if kind == FEATURES and target.get("layer") not in (None, *LAYERS):
        problems.append(f"target.layer is one of {', '.join(LAYERS)}")
    if kind == GOAL and not re.fullmatch(goals.REF, str(target.get("goal", "G-1"))):
        problems.append("target.goal is a goal's code (G-<number>) or its alias")
    if kind == PROBLEM and not re.fullmatch(problem_model.REF, str(target.get("problem", "P-1"))):
        problems.append("target.problem is a problem's code (P-<number>)")
    if kind == OPPORTUNITY:
        problems.extend(opportunity_changes.target_problems(target))
    if kind == SYSTEM and "system" in target and (why := system_spec.check_slug(target["system"])) is not None:
        problems.append(f"target.system: {why}")
    lines, said = _evidence_of(kind, arguments.get("evidence"))
    problems.extend(said)
    if kind in ACTIONS:
        problems.extend(plugin_changes.field_problems(kind, sets, unset))
        if not problems:
            sets = plugin_changes.values(sets)
    elif lines and not sets and not unset:
        if "problem" not in target and kind == PROBLEM:
            problems.append("a proposal of evidence alone names the problem: target.problem")
        if "opportunity" not in target and kind == OPPORTUNITY:
            problems.append("a proposal of evidence alone names the opportunity: target.opportunity")
    else:
        problems.extend(_field_problems(sets, unset))
    if problems:
        raise ProposalError(422, f"{'; '.join(problems)}; nothing proposed")
    return Change(kind, dict(target), dict(sets), tuple(unset), summary, tuple(lines))


# What each kind does


KINDS: dict[str, Kind] = {  # kind -> where its change applies, how it is checked, applied and rolled back
    BRANDING: setting_changes.SETTINGS,
    FEATURES: setting_changes.SETTINGS,
    PLUGIN_CONFIG: setting_changes.CONFIG,
    AGENT: setting_changes.AGENT_PROFILE,
    POLICY: setting_changes.AUTONOMY_POLICY,
    PROBLEM: problem_changes.KIND,
    GOAL: goal_changes.KIND,
    OPPORTUNITY: opportunity_changes.KIND,
    SYSTEM: system_changes.KIND,
    **dict.fromkeys(ACTIONS, plugin_changes.KIND),
}


async def locate(conn: AsyncConnection, access: ProjectAccess, change: Change, *, lock: bool = False) -> Target:
    """Where ``change`` applies, as it is now; ProposalError when the project has no such place. ``lock``: for a
    write."""
    return await KINDS[change.kind].locate(conn, access, change, lock)


async def check(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str) -> None:
    """ProposalError (422) naming what is wrong with ``change`` as the next version of ``target``."""
    await KINDS[change.kind].check(conn, access, change, target, hub_version)


# Applying and rolling back


def _still_at(target: Target, version: int, doing: str) -> None:
    if target.version != version:
        raise ProposalError(
            409, f"{target.title} is at version {target.version} now, not {version}: {doing}; {NOTHING}"
        )


async def apply(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    change: Change,
    base: int,
    hub_version: str,
    proposal_id: int = 0,
) -> int:
    """Write ``change`` (of proposal ``proposal_id``) as the next version of its setting, which must still be at
    version ``base``; that version."""
    target = await locate(conn, access, change, lock=True)
    _still_at(target, base, "it changed since the proposal, so propose the change again")
    return await KINDS[change.kind].apply(Writing(conn, user, access, change, target, base, hub_version, proposal_id))


async def roll_back(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    change: Change,
    applied: int,
    hub_version: str,
    proposal_id: int = 0,
) -> int:
    """Make the next version of the setting ``change`` (of proposal ``proposal_id``) made version ``applied`` of a
    copy of the one before, while it is still at ``applied``; that version."""
    target = await locate(conn, access, change, lock=True)
    _still_at(target, applied, "it changed since the proposal was applied, so roll it back by hand")
    return await KINDS[change.kind].roll_back(
        Writing(conn, user, access, change, target, applied, hub_version, proposal_id)
    )


# Deciding


def refusal(user: Principal, access: ProjectAccess, kind: str, doing: str, own: bool = False) -> str | None:
    """Why ``user`` may not do ``doing`` to a proposal of ``kind``; None when they may. ``own`` (``is_own``): a hub
    admin or an admin of the project may."""
    if user.acts_for_run is not None:
        return f"{doing} is for an admin, never for the agent of a run"
    if own and user.admin:
        return None
    if kind in HUB_WIDE and not own:
        return None if user.admin else f"{doing} needs a hub admin, and {user.login} is none"
    role = "writer" if kind in BY_WRITERS else "admin"
    if has_role(access.role, role):
        return None
    return f"{doing} needs the {role} role on project {access.name}, and {user.login} holds {access.role or 'no grant'}"


def _said(exc: Exception) -> str:
    text = exc.message if isinstance(exc, ProposalError) else str(getattr(exc, "detail", exc))
    return " ".join(text.split())[:MAX_ERROR_CHARS]


async def _record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, what: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name}/builder:proposal:{what}",
        project_id=access.project_id,
    )


async def decide(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    proposal_id: int,
    answer: str,
    hub_version: str,
    install: plugin_changes.Install | None = None,
) -> BuilderProposal:
    """Approve or reject pending proposal ``proposal_id``: approved, the hub applies it, or keeps why it could not;
    ProposalError when ``user`` may not, or it is no longer pending. ``install`` installs a release, for a proposal
    of kind plugin_install."""
    row = await _locked(conn, access, proposal_id)
    doing = f"{'approving' if answer == APPROVE else 'rejecting'} proposal {proposal_id}"
    own = await own_install(conn, access, row.kind, row.change.get("set", {}))
    refused = refusal(user, access, row.kind, doing, own)
    if refused is not None:
        raise ProposalError(403, refused)
    if row.state != PENDING:
        raise ProposalError(409, f"proposal {proposal_id} is {row.state.replace('_', ' ')} already; {NOTHING}")
    values = {"decided_by": user.user_id, "decided_at": func.now(), "state": REJECTED}
    if answer == APPROVE and row.kind in ACTIONS:
        await plugin_changes.ready(conn, user, row)
        try:
            async with conn.begin_nested():
                done = await plugin_changes.apply(conn, user, access, row, row.change["set"], install, own)
            values |= {"state": APPLIED, "applied_version": plugin_changes.DONE}
            values |= {"opened_run_id": done.run_id, "outcome": done.outcome}
        except (HTTPException, ProposalError) as exc:
            values |= {"state": FAILED, "error": _said(exc)}
    elif answer == APPROVE:
        try:
            async with conn.begin_nested():
                version = await apply(conn, user, access, change_of_row(row), row.base_version, hub_version, row.id)
            values |= {"state": APPLIED, "applied_version": version}
        except (HTTPException, ProposalError) as exc:
            values |= {"state": FAILED, "error": _said(exc)}
    b = tables.builder_proposals
    await conn.execute(update(b).where(b.c.id == row.id).values(**values))
    await _record(conn, user, access, AUDIT_DECIDE, f"{proposal_id} {answer} state={values['state']}")
    log.info("builder proposal decided", extra={"proposal": proposal_id, "state": values["state"]})
    return await shown(conn, access, proposal_id)


async def rollback(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, proposal_id: int, hub_version: str
) -> BuilderProposal:
    """Roll back applied proposal ``proposal_id``; ProposalError when ``user`` may not, or the hub cannot."""
    row = await _locked(conn, access, proposal_id)
    refused = refusal(user, access, row.kind, f"rolling back proposal {proposal_id}")
    if refused is not None:
        raise ProposalError(403, refused)
    if row.state != APPLIED:
        raise ProposalError(
            409, f"proposal {proposal_id} is {row.state.replace('_', ' ')}: only an applied one rolls back"
        )
    if row.kind in ACTIONS:
        raise ProposalError(
            409,
            f"proposal {proposal_id} is an action ({row.kind.replace('_', ' ')}), done once, which never rolls back: "
            f"undo it by hand; {NOTHING}",
        )
    try:
        async with conn.begin_nested():
            version = await roll_back(conn, user, access, change_of_row(row), row.applied_version, hub_version, row.id)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, _said(exc)) from None
    b = tables.builder_proposals
    await conn.execute(update(b).where(b.c.id == row.id).values(state=ROLLED_BACK, rollback_version=version))
    await _record(conn, user, access, AUDIT_DECIDE, f"{proposal_id} rollback state={ROLLED_BACK}")
    log.info("builder proposal rolled back", extra={"proposal": proposal_id, "version": version})
    return await shown(conn, access, proposal_id)


def done_already(row, verb: str, user_id: int) -> bool:
    """Whether ``verb`` by ``user_id`` is what proposal ``row`` shows done already: a delivery of the same answer."""
    if verb == ROLLBACK:
        return row.state == ROLLED_BACK
    decided = {APPROVE: (APPLIED, FAILED, ROLLED_BACK), REJECT: (REJECTED,)}[verb]
    return row.state in decided and row.decided_by == user_id


# Proposing


def thread_text(proposal_id: int, change: Change, target: Target, approver: str) -> str:
    """The hub's message that shows a proposal in its thread."""
    shown = change.sets | ({"evidence": list(change.evidence)} if change.evidence else {})
    shown_values = json.dumps(shown, ensure_ascii=False, indent=1, sort_keys=True)
    if len(shown_values.encode()) > MAX_SHOWN_BYTES:
        shown_values = shown_values.encode()[:MAX_SHOWN_BYTES].decode(errors="ignore") + "\n..."
    sets = ", ".join(sorted(change.sets)) or "nothing"
    unsets = ", ".join(change.unset) or "nothing"
    lines = f" It adds {len(change.evidence)} lines of evidence." if change.evidence else ""
    if change.kind == SYSTEM:
        lines = f" {system_changes.described(change, target)} The designer shows it on the canvas."
    if change.kind in ACTIONS:
        return (
            f"Proposal {proposal_id} of @builder: {change.summary}\n\n"
            f"It changes {target.title}. {ACTION_WORDS[change.kind]}\n\n"
            f"```json\n{shown_values}\n```\n\n"
            f"Nothing is done until {approver} approves it: reply `approve {proposal_id}` or `reject {proposal_id}` in "
            "this thread, or answer it from the Inbox. Approving does it once; it does not roll back."
        )
    return (
        f"Proposal {proposal_id} of @builder: {change.summary}\n\n"
        f"It changes {target.title}, now at version {target.version}: it sets {sets} and unsets {unsets}.{lines}\n\n"
        f"```json\n{shown_values}\n```\n\n"
        f"Nothing changes until {approver} approves it: reply `approve {proposal_id}` or `reject {proposal_id}` in "
        f"this thread, or answer it from the Inbox. Once it is applied, `rollback {proposal_id}` brings back the "
        "version before it."
    )


ACTION_WORDS = {
    PLUGIN: "Approving it hands @builder the task in this thread, for the member who approves: create the repo from "
    "the plugin template, write and test the plugin, push it so the repo's CI signs and releases it, add the repo to "
    "the project, then propose its install and its listing.",
    PLUGIN_INSTALL: "Approving it downloads the release, trusts its signer for its publisher if this hub does not yet, "
    "and installs the plugin on this hub.",
    PLUGIN_LISTING: "Approving it hands @builder the task in this thread, for the member who approves: a pull request "
    "that adds a free plugin to the market's index, or a paid plugin's listing and bundle on the seller's hub.",
}


def approver_words(kind: str, project: str, own: bool = False) -> str:
    if own:
        return f"an admin of project {project} (or a hub admin)"
    if kind in HUB_WIDE:
        return "a hub admin"
    return f"a writer of project {project}" if kind in BY_WRITERS else f"an admin of project {project}"


async def approvers(
    conn: AsyncConnection, access: ProjectAccess, kind: str, admins: frozenset[str], own: bool = False
) -> list[int]:
    """The members who may decide a proposal of ``kind`` in the project: the hub admins who signed in, the project's
    writers and admins for a kind of BY_WRITERS, or the project's admins; for an ``own`` install, the project's admins."""
    u, g = tables.users, tables.grants
    if kind in HUB_WIDE and not own:
        logins = literal(sorted(admins), ARRAY(Text))
        query = select(u.c.id).where(admin_login(u) == any_(logins))
    else:
        roles = ("writer", "admin") if kind in BY_WRITERS else ("admin",)
        query = select(g.c.user_id).where(g.c.project_id == access.project_id, g.c.role.in_(roles))
    return sorted(set((await conn.execute(query)).scalars()))


async def store(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, run) -> int:
    """Keep ``change`` as a pending proposal of chat run ``run`` (``chat_runs.ChatRun``), or of the Curator's review
    run (``server.ReviewRun``, no chat and no thread); its id."""
    b = tables.builder_proposals
    values = {
        "project_id": access.project_id,
        "run_id": run.run_id,
        "chat_id": run.chat_id,
        "thread_root_id": run.root_id,
        "kind": change.kind,
        "target": {**change.target, **target.names},
        "change": change.stored(),
        "summary": change.summary,
        "base_version": target.version,
        "proposed_by": run.dispatcher_id,
    }
    return (await conn.execute(insert(b).values(values).returning(b.c.id))).scalar_one()


async def shown_at(conn: AsyncConnection, proposal_id: int, message_id: int) -> None:
    b = tables.builder_proposals
    await conn.execute(update(b).where(b.c.id == proposal_id).values(message_id=message_id))


async def record_proposal(conn: AsyncConnection, user: Principal, access: ProjectAccess, proposal_id: int, run) -> None:
    await _record(conn, user, access, AUDIT_PROPOSE, f"{proposal_id} run:{run.run_id}")


async def thread_of(conn: AsyncConnection, proposal_id: int) -> tuple[int, int] | None:
    """The chat and the root of the thread proposal ``proposal_id`` was made in, while both are there."""
    b = tables.builder_proposals
    query = select(b.c.chat_id, b.c.thread_root_id).where(b.c.id == proposal_id)
    row = (await conn.execute(query)).first()
    return (
        None if row is None or row.chat_id is None or row.thread_root_id is None else (row.chat_id, row.thread_root_id)
    )
