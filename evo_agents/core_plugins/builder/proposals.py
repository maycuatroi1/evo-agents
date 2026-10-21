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
- ``goal``: a goal of the project (``evo_agents.hub.goals``), new or one that exists (``target.goal``, its code or
  alias), its fields named as the goal's route names them (statement, metric, deadline, canvas, rank, owner, status,
  close_reason); its version is the goal's revision, 0 for a new one, whose code the hub picks when it is proposed and
  keeps in the target. An admin of the project decides it, and the hub writes the next revision as the goal's route
  does (``goals.write``); rolling back a new goal closes it (dropped).

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import ARRAY, Text, any_, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder import MAX_FIELDS, MAX_SUMMARY_CHARS
from evo_agents.hub import autonomy, branding, chats, features, goals, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import audit, autonomy_policies, hub_settings, plugin_configs
from evo_agents.hub.server import goals as project_goals
from evo_agents.hub.server.agent_packs import AgentPackPin, pinned
from evo_agents.hub.server.agent_profiles import AgentEdit, new_version, profile_changes
from evo_agents.hub.server.plugin_grants import installed_row
from evo_agents.hub.server.profile_store import Profile, by_handle, version_rules
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_settings import PROPOSAL_KINDS, PROPOSAL_STATES
from evo_agents.plugins import config as plugin_config

log = logging.getLogger(__name__)

BRANDING, FEATURES, PLUGIN_CONFIG, AGENT, POLICY, GOAL = PROPOSAL_KINDS
PENDING, APPLIED, REJECTED, FAILED, ROLLED_BACK = PROPOSAL_STATES
HUB_WIDE = (BRANDING, FEATURES)  # a hub admin decides them; an admin of the project the others
APPROVE, REJECT, ROLLBACK = "approve", "reject", "rollback"
BRANDING_FIELDS = tuple(name for name in branding.FIELDS if name not in branding.ASSETS)
AGENT_FIELDS = ("name", "description", "instructions", "packs")
LAYERS = ("hub", "project")  # target.layer of a proposal of features
TARGET_KEYS = {  # kind -> (the keys its target needs, those it may have)
    BRANDING: ((), ()),
    FEATURES: (("layer",), ()),
    PLUGIN_CONFIG: (("plugin",), ("agent",)),
    AGENT: (("agent",), ()),
    POLICY: ((), ()),
    GOAL: ((), ("goal",)),
}
MAX_ERROR_CHARS = 2000
MAX_SHOWN_BYTES = 3000  # of the values a proposal sets, in its message in the thread
NOTICE = "builder_proposal"  # evo_agents.hub.runs.NOTICE_KINDS
AUDIT_PROPOSE = "builder.propose"  # the agent of a chat run of @builder proposed a change, for the run's dispatcher
AUDIT_DECIDE = "builder.decide"  # an admin approved, rejected or rolled back a proposal
NOTHING = "nothing changed"


class ProposalError(Exception):
    """Why a proposal cannot be made, decided or rolled back, with the HTTP status a route answers; nothing changed."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Change:
    """What a proposal changes: its kind, where (``target``), the fields it sets and those it unsets."""

    kind: str
    target: dict
    sets: dict
    unset: tuple[str, ...]
    summary: str = ""

    def stored(self) -> dict:
        return {"set": self.sets, "unset": list(self.unset)}


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
    names: dict = field(default_factory=dict)  # what the hub adds to the target it keeps: a goal's code


class BuilderProposal(BaseModel):
    """A change @builder proposed, and what became of it."""

    id: int
    project: str
    kind: Literal[PROPOSAL_KINDS]
    target: dict[str, str] = Field(
        description="layer (features), plugin and agent (plugin_config), agent (agent), goal (goal: its code, which "
        "the hub picks for a new one); nothing for policy"
    )
    change: dict[str, Any] = Field(
        description="set: field -> value; unset: the fields it clears; a key of the policy by its dotted path"
    )
    summary: str
    base_version: int = Field(description="the setting's version it was proposed on, which approving needs still")
    state: Literal[PROPOSAL_STATES]
    approver: Literal["hub_admin", "project_admin"] = Field(description="who may decide it")
    proposed_by: str = Field(description="the admin who asked @builder, the run's dispatcher")
    proposed_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    applied_version: int | None = Field(description="the setting's version approving made")
    rollback_version: int | None = Field(description="the setting's version rolling back made")
    error: str | None = Field(description="why the hub could not apply it, for a failed one")
    run_id: int | None
    link: str | None = Field(description="the hub's message that shows it in its thread")


# Reading


def _select():
    b, p, u = tables.builder_proposals, tables.projects, tables.users
    proposer, decider = u.alias("proposer"), u.alias("decider")
    return (
        select(b, p.c.name.label("project"), proposer.c.login.label("proposer"), decider.c.login.label("decider"))
        .join_from(b, p, p.c.id == b.c.project_id)
        .join(proposer, proposer.c.id == b.c.proposed_by)
        .outerjoin(decider, decider.c.id == b.c.decided_by)
    )


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
        approver="hub_admin" if row.kind in HUB_WIDE else "project_admin",
        proposed_by=row.proposer,
        proposed_at=row.proposed_at,
        decided_by=row.decider,
        decided_at=row.decided_at,
        applied_version=row.applied_version,
        rollback_version=row.rollback_version,
        error=row.error,
        run_id=row.run_id,
        link=link,
    )


async def listed(
    conn: AsyncConnection, project_id: int, state: str | None = None, limit: int = 100
) -> list[BuilderProposal]:
    """The proposals of the project, the newest first; those in ``state`` alone when given."""
    b = tables.builder_proposals
    query = _select().where(b.c.project_id == project_id)
    if state is not None:
        query = query.where(b.c.state == state)
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
    return Change(row.kind, dict(row.target), dict(row.change["set"]), tuple(row.change["unset"]), row.summary)


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
    problems.extend(_field_problems(sets, unset))
    if problems:
        raise ProposalError(422, f"{'; '.join(problems)}; nothing proposed")
    return Change(kind, dict(target), dict(sets), tuple(unset), summary)


# Where a change applies


async def _settings_target(conn: AsyncConnection, access: ProjectAccess, change: Change) -> Target:
    if change.kind == BRANDING:
        place = hub_settings.Place(hub_settings.BRANDING)
    elif change.target["layer"] == "hub":
        place = hub_settings.Place(hub_settings.FEATURES)
    else:
        place = hub_settings.Place(hub_settings.FEATURES, access.project_id, access.name)
    row = await hub_settings.current(conn, place)
    held = dict(row.plain) if row is not None else {}
    return Target(place.title(), 0 if row is None else row.version, held, settings=place)


async def _profile(conn: AsyncConnection, access: ProjectAccess, handle: str, *, lock: bool = False) -> Profile:
    profile = await by_handle(conn, handle, lock=lock)
    if profile is None or profile.project_id != access.project_id or profile.deleted_at is not None:
        raise ProposalError(
            404,
            f"project {access.name} has no agent @{handle}: see `evo-agents hub agent list --project {access.name}`",
        )
    return profile


async def _config_target(conn: AsyncConnection, access: ProjectAccess, change: Change) -> Target:
    name, handle = change.target["plugin"], change.target.get("agent")
    plugin = await enabled_in(conn, name, access.project_id)
    if plugin is None:
        raise ProposalError(409, f"plugin {name} is not enabled in project {access.name}, or it is turned off")
    place = plugin_configs.place_of(plugin, "project", project_id=access.project_id, project=access.name)
    title = f"the config of plugin {name} in project {access.name}"
    if handle is not None:
        profile = await _profile(conn, access, handle)
        place = plugin_configs.place_of(
            plugin, "agent", project_id=access.project_id, project=access.name, profile_id=profile.id, handle=handle
        )
        title = f"the config of plugin {name} for @{handle} in project {access.name}"
    row = await plugin_configs.current(conn, place)
    held = {**dict.fromkeys(row.secrets), **row.plain} if row is not None else {}
    return Target(title, 0 if row is None else row.version, held, config=place, schema=plugin.manifest.get("config"))


async def _agent_target(conn: AsyncConnection, access: ProjectAccess, change: Change, *, lock: bool) -> Target:
    profile = await _profile(conn, access, change.target["agent"], lock=lock)
    if profile.is_default:
        raise ProposalError(
            422, f"@{profile.handle} is the default agent of project {access.name}, which @builder does not change"
        )
    return Target(
        f"agent @{profile.handle} of project {access.name}", profile.version, profile.snapshot(), profile=profile
    )


async def _policy_target(conn: AsyncConnection, access: ProjectAccess) -> Target:
    row = await autonomy_policies.newest(conn, access.project_id)
    body = {} if row is None else dict(row.body)
    title = f"the autonomy policy of project {access.name}"
    return Target(title, 0 if row is None else row.revision, autonomy.flat(body), policy=body)


async def _goal_target(conn: AsyncConnection, access: ProjectAccess, change: Change) -> Target:
    """Goal ``target.goal`` as it is now, or a new goal under the next code when the target names none or that one."""
    await project_goals.adopt_charter_goals(conn, access.project_id, access.name)
    ref = change.target.get("goal")
    row = await project_goals.found(conn, access.project_id, ref) if ref is not None else None
    if row is None:
        code = await project_goals.next_code(conn, access.project_id)
        if ref is not None and ref != code:
            raise ProposalError(404, f"project {access.name} has no goal {ref}; a new goal takes the code {code}")
        return Target(f"a new goal {code} of project {access.name}", 0, {}, goal={}, names={"goal": code})
    code, fields = goals.code(row.number), project_goals.fields_of(row)
    held = {name: value for name, value in fields.items() if value is not None}
    return Target(f"goal {code} of project {access.name}", row.revision, held, goal=fields, names={"goal": code})


async def locate(conn: AsyncConnection, access: ProjectAccess, change: Change, *, lock: bool = False) -> Target:
    """Where ``change`` applies, as it is now; ProposalError when the project has no such place."""
    if change.kind in HUB_WIDE:
        return await _settings_target(conn, access, change)
    if change.kind == PLUGIN_CONFIG:
        return await _config_target(conn, access, change)
    if change.kind == POLICY:
        return await _policy_target(conn, access)
    if change.kind == GOAL:
        return await _goal_target(conn, access, change)
    return await _agent_target(conn, access, change, lock=lock)


# Checking a change


def _values(change: Change) -> dict:
    return branding.normalized(change.sets) if change.kind == BRANDING else change.sets


def _layer_problems(change: Change, target: Target) -> list[str]:
    """Each field ``change`` unsets that the layer does not set, or that it changes nothing."""
    problems = [
        f"{name}: the layer does not set it, so there is nothing to unset"
        for name in change.unset
        if name not in target.held
    ]
    same = all(target.held.get(name) == value for name, value in _values(change).items())
    if not problems and not change.unset and same:
        problems.append(f"{target.title} has these values already: it changes nothing")
    return problems


def _branding_problems(change: Change, target: Target) -> list[str]:
    images = sorted(set(change.sets) & set(branding.ASSETS))
    if images:
        return [f"{name}: an image, which an admin uploads; @builder may unset it, never set it" for name in images]
    values = _values(change)
    after = {name: value for name, value in target.held.items() if name not in change.unset} | values
    return branding.problems(values, after)


def _features_problems(change: Change, target: Target) -> list[str]:
    return features.problems(change.sets, features.INSTANCE if target.settings.project_id is None else features.PROJECT)


def _config_problems(change: Change, target: Target) -> list[str]:
    secret = plugin_config.fields(target.schema)
    secrets = sorted(name for name in change.sets if secret.get(name))
    if secrets:
        return [
            f"{name}: a secret, which @builder never sets, since it would see its value; an admin sets it with "
            "`evo-agents hub plugin config`"
            for name in secrets
        ]
    return plugin_config.check_values(target.schema, change.sets)


async def _agent_changes(
    conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> dict:
    """The columns ``change`` sets on the profile of ``target``, checked as an edit of the profile is."""
    extra = sorted(set(change.sets) - set(AGENT_FIELDS))
    if extra or change.unset:
        raise ProposalError(
            422,
            f"an agent proposal sets {', '.join(AGENT_FIELDS)} and unsets nothing; {', '.join(extra or change.unset)}",
        )
    try:
        body = AgentEdit.model_validate(change.sets)
    except ValidationError as exc:
        said = "; ".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in exc.errors())
        raise ProposalError(422, said) from None
    try:
        packs = await pinned(conn, access.project_id, access.name, body.packs, hub_version)
        changes = profile_changes(target.profile, body, packs)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    if not changes:
        raise ProposalError(422, f"{target.title} has these values already: it changes nothing")
    return changes


async def _policy_body(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target) -> dict:
    """The policy ``change`` makes of the revision ``target`` holds, checked as a write of the policy is."""
    body, problems = autonomy.with_changes(target.policy, change.sets, change.unset)
    if not problems:
        problems = _layer_problems(change, target)
    if not problems:
        problems = autonomy.problems(body, await autonomy_policies.project_repos(conn, access.project_id))
    if problems:
        raise ProposalError(422, f"{'; '.join(problems)}; {NOTHING}")
    return body


def _said_by_pydantic(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, error['loc'])) or 'goal'}: {error['msg']}" for error in exc.errors())


def _goal_body(change: Change, target: Target) -> project_goals.ProjectGoalWrite:
    """The goal ``change`` makes of the one ``target`` holds, as a write of its route; ProposalError (422) naming
    what is wrong with its shape."""
    problems = [f"{name}: a goal has no such field" for name in sorted(set(change.sets) - set(goals.FIELDS))]
    problems += [f"{name}: a goal always has one" for name in change.unset if name not in goals.CLEARABLE]
    if problems:
        raise ProposalError(422, f"{'; '.join(problems)}; {NOTHING}")
    fields = {**target.goal, **change.sets, **dict.fromkeys(change.unset)}
    if change.sets.get("status", goals.CLOSED) != goals.CLOSED and "close_reason" not in change.sets:
        fields["close_reason"] = None  # opened again
    try:
        return project_goals.ProjectGoalWrite.model_validate(fields)
    except ValidationError as exc:
        raise ProposalError(422, f"{_said_by_pydantic(exc)}; {NOTHING}") from None


async def _goal_problems(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target) -> list[str]:
    body = _goal_body(change, target)
    problems = _layer_problems(change, target) if target.version else []
    try:
        return problems + await project_goals.check(conn, access, target.names["goal"], body)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None


CHECKS: dict[str, Callable[[Change, Target], list[str]]] = {
    BRANDING: _branding_problems,
    FEATURES: _features_problems,
    PLUGIN_CONFIG: _config_problems,
}


async def check(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, hub_version: str) -> None:
    """ProposalError (422) naming what is wrong with ``change`` as the next version of ``target``."""
    if change.kind == AGENT:
        await _agent_changes(conn, access, change, target, hub_version)
        return
    if change.kind == POLICY:
        await _policy_body(conn, access, change, target)
        return
    if change.kind == GOAL:
        problems = await _goal_problems(conn, access, change, target)
    else:
        problems = CHECKS[change.kind](change, target) or _layer_problems(change, target)
    if problems:
        raise ProposalError(422, f"{'; '.join(problems)}; {NOTHING}")


# Applying and rolling back


def _no_problems(sets: dict, after: dict) -> list[str]:
    return []


async def _apply_settings(conn: AsyncConnection, user: Principal, change: Change, target: Target, base: int) -> int:
    def checked(sets: dict, after: dict) -> list[str]:
        return CHECKS[change.kind](change, target)

    place = target.settings
    return (await hub_settings.apply(conn, user, place, _values(change), list(change.unset), base, checked)).version


async def _apply_config(conn: AsyncConnection, user: Principal, change: Change, target: Target, base: int) -> int:
    body = plugin_configs.PluginConfigWrite(values=change.sets, unset=list(change.unset), if_version=base)
    return (await plugin_configs.write_layer(conn, user, target.config, target.schema, body, None)).version


async def _write_profile(conn: AsyncConnection, user: Principal, access: ProjectAccess, profile, changes: dict) -> int:
    version = await new_version(conn, profile, changes, user.user_id)
    target = f"{access.name}/@{profile.handle} v{version} fields={','.join(sorted(changes))}"
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.AGENT_UPDATE,
        target=target,
        project_id=access.project_id,
    )
    return version


async def _write_policy(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, body: dict, base: int, note: str, via: str
) -> int:
    """Write ``body`` as the policy's next revision while it is at revision ``base``; that revision."""
    revision, written = await autonomy_policies.write(conn, user, access, body, note=note, if_revision=base, via=via)
    if not written:
        raise ProposalError(409, f"the autonomy policy of project {access.name} is this already; {NOTHING}")
    return revision


async def _write_goal(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    target: Target,
    body: project_goals.ProjectGoalWrite,
    base: int,
    note: str,
    via: str,
) -> int:
    """Write ``body`` as the next revision of the goal of ``target`` while it is at revision ``base``; that one."""
    ref = target.names["goal"]
    body = body.model_copy(update={"note": note[: goals.MAX_NOTE_CHARS], "if_revision": base})
    try:
        shown, written = await project_goals.write(conn, user, access, ref, body, via=via)
    except HTTPException as exc:
        raise ProposalError(exc.status_code, str(exc.detail)) from None
    if not written:
        raise ProposalError(409, f"goal {ref} of project {access.name} is this already; {NOTHING}")
    return shown.revision


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
    if change.kind == PLUGIN_CONFIG:
        await installed_row(conn, access, change.target["plugin"], lock=True)  # one write of its config at a time
    target = await locate(conn, access, change, lock=True)
    _still_at(target, base, "it changed since the proposal, so propose the change again")
    if change.kind == AGENT:
        changes = await _agent_changes(conn, access, change, target, hub_version)
        return await _write_profile(conn, user, access, target.profile, changes)
    if change.kind == POLICY:
        body = await _policy_body(conn, access, change, target)
        note = f"proposal {proposal_id} of @builder: {change.summary}"
        return await _write_policy(conn, user, access, body, base, note, f" proposal={proposal_id}")
    if change.kind == GOAL:
        note = f"proposal {proposal_id} of @builder: {change.summary}"
        body = _goal_body(change, target)
        return await _write_goal(conn, user, access, target, body, base, note, f" proposal={proposal_id}")
    await check(conn, access, change, target, hub_version)
    if change.kind == PLUGIN_CONFIG:
        return await _apply_config(conn, user, change, target, base)
    return await _apply_settings(conn, user, change, target, base)


async def _roll_back_layer(conn: AsyncConnection, user: Principal, target: Target, applied: int) -> int:
    """The next version of the layer of ``target``, a copy of version ``applied - 1``; version 0 is no layer."""
    before = applied - 1
    if target.settings is not None:
        if before >= 1:
            body = hub_settings.SettingsRollback(version=before, if_version=applied)
            return (await hub_settings.rollback_layer(conn, user, target.settings, body)).version
        layer = await hub_settings.apply(conn, user, target.settings, {}, sorted(target.held), applied, _no_problems)
        return layer.version
    if before >= 1:
        body = plugin_configs.PluginConfigRollback(version=before, if_version=applied)
        return (await plugin_configs.rollback_layer(conn, user, target.config, body)).version
    body = plugin_configs.PluginConfigWrite(unset=sorted(target.held), if_version=applied)
    return (await plugin_configs.write_layer(conn, user, target.config, target.schema, body, None)).version


async def _roll_back_profile(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, change: Change, target: Target, hub_version: str
) -> int:
    profile = target.profile
    before = await version_rules(conn, profile.id, profile.version - 1)
    if before is None:
        raise ProposalError(409, f"{target.title} has no version {profile.version - 1}; {NOTHING}")
    wanted = {name: before[name] for name in change.sets if name != "packs"}
    if "packs" in change.sets:
        pins = [AgentPackPin(**item) for item in before["packs"] or ()]
        try:
            wanted["packs"] = await pinned(conn, access.project_id, access.name, pins, hub_version)
        except HTTPException as exc:
            raise ProposalError(exc.status_code, str(exc.detail)) from None
    now = profile.snapshot()
    changes = {name: value for name, value in wanted.items() if now[name] != value}
    if not changes:
        raise ProposalError(409, f"{target.title} is as it was before the proposal already; {NOTHING}")
    return await _write_profile(conn, user, access, profile, changes)


async def _roll_back_policy(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, applied: int, proposal_id: int
) -> int:
    """The policy's next revision, a copy of revision ``applied - 1``; revision 0 is no policy."""
    before = applied - 1
    body = await autonomy_policies.body_at(conn, access.project_id, before)
    if body is None:
        raise ProposalError(409, f"the autonomy policy of project {access.name} has no revision {before}; {NOTHING}")
    note = f"rollback of proposal {proposal_id} of @builder: a copy of revision {before}"
    return await _write_policy(conn, user, access, body, applied, note, f" proposal={proposal_id} copy-of={before}")


async def _roll_back_goal(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, target: Target, applied: int, proposal_id: int
) -> int:
    """The goal's next revision, a copy of revision ``applied - 1``; a goal the proposal made is closed (dropped)."""
    ref, before = target.names["goal"], applied - 1
    row = await project_goals.found(conn, access.project_id, ref, before or None)
    if row is None:
        raise ProposalError(409, f"goal {ref} of project {access.name} has no revision {before}; {NOTHING}")
    if before == 0:
        body = project_goals.closing(row, "dropped", None, applied)
        note = f"rollback of proposal {proposal_id} of @builder: the goal it made, closed"
    else:
        body = project_goals.ProjectGoalWrite.model_validate(project_goals.fields_of(row))
        note = f"rollback of proposal {proposal_id} of @builder: a copy of revision {before}"
    via = f" proposal={proposal_id} copy-of={before}"
    return await _write_goal(conn, user, access, target, body, applied, note, via)


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
    if change.kind == PLUGIN_CONFIG:
        await installed_row(conn, access, change.target["plugin"], lock=True)
    target = await locate(conn, access, change, lock=True)
    _still_at(target, applied, "it changed since the proposal was applied, so roll it back by hand")
    if change.kind == AGENT:
        return await _roll_back_profile(conn, user, access, change, target, hub_version)
    if change.kind == POLICY:
        return await _roll_back_policy(conn, user, access, applied, proposal_id)
    if change.kind == GOAL:
        return await _roll_back_goal(conn, user, access, target, applied, proposal_id)
    return await _roll_back_layer(conn, user, target, applied)


# Deciding


def refusal(user: Principal, access: ProjectAccess, kind: str, doing: str) -> str | None:
    """Why ``user`` may not do ``doing`` to a proposal of ``kind``; None when they may."""
    if user.scope is not None:
        return f"{doing} is for an admin, never for the agent of a run"
    if kind in HUB_WIDE:
        return None if user.admin else f"{doing} needs a hub admin, and {user.login} is none"
    if has_role(access.role, "admin"):
        return None
    return f"{doing} needs the admin role on project {access.name}, and {user.login} holds {access.role or 'no grant'}"


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
    conn: AsyncConnection, user: Principal, access: ProjectAccess, proposal_id: int, answer: str, hub_version: str
) -> BuilderProposal:
    """Approve or reject pending proposal ``proposal_id``: approved, the hub applies it, or keeps why it could not;
    ProposalError when ``user`` may not, or it is no longer pending."""
    row = await _locked(conn, access, proposal_id)
    doing = f"{'approving' if answer == APPROVE else 'rejecting'} proposal {proposal_id}"
    refused = refusal(user, access, row.kind, doing)
    if refused is not None:
        raise ProposalError(403, refused)
    if row.state != PENDING:
        raise ProposalError(409, f"proposal {proposal_id} is {row.state.replace('_', ' ')} already; {NOTHING}")
    values = {"decided_by": user.user_id, "decided_at": func.now(), "state": REJECTED}
    if answer == APPROVE:
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
    shown_values = json.dumps(change.sets, ensure_ascii=False, indent=1, sort_keys=True)
    if len(shown_values.encode()) > MAX_SHOWN_BYTES:
        shown_values = shown_values.encode()[:MAX_SHOWN_BYTES].decode(errors="ignore") + "\n..."
    sets = ", ".join(sorted(change.sets)) or "nothing"
    unsets = ", ".join(change.unset) or "nothing"
    return (
        f"Proposal {proposal_id} of @builder: {change.summary}\n\n"
        f"It changes {target.title}, now at version {target.version}: it sets {sets} and unsets {unsets}.\n\n"
        f"```json\n{shown_values}\n```\n\n"
        f"Nothing changes until {approver} approves it: reply `approve {proposal_id}` or `reject {proposal_id}` in "
        f"this thread, or answer it from the Inbox. Once it is applied, `rollback {proposal_id}` brings back the "
        "version before it."
    )


def approver_words(kind: str, project: str) -> str:
    return "a hub admin" if kind in HUB_WIDE else f"an admin of project {project}"


async def approvers(conn: AsyncConnection, access: ProjectAccess, kind: str, admins: frozenset[str]) -> list[int]:
    """The members who may decide a proposal of ``kind`` in the project: the hub admins who signed in, or the
    project's admins."""
    u, g = tables.users, tables.grants
    if kind in HUB_WIDE:
        logins = literal(sorted(admins), ARRAY(Text))
        query = select(u.c.id).where(func.lower(u.c.login) == any_(logins))
    else:
        query = select(g.c.user_id).where(g.c.project_id == access.project_id, g.c.role == "admin")
    return sorted(set((await conn.execute(query)).scalars()))


async def store(conn: AsyncConnection, access: ProjectAccess, change: Change, target: Target, run) -> int:
    """Keep ``change`` as a pending proposal of chat run ``run`` (``chat_runs.ChatRun``); its id."""
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
