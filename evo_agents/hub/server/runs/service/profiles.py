"""The agent profile of a run (``evo_agents.hub.profiles``) where runs are dispatched, rerun, claimed and handed out.

A dispatch names a profile by handle or none, which means the project's default (``profile_store.ensure_default``).
``choose`` first refuses a runtime the project does not register (``plugin_runtimes.check``), then resolves the
profile and checks what the dispatch asks against its allowlists, each refusal a 422 that says which allowlist leaves
what out: the run's project (a profile of another project needs the dispatcher to be a
writer there too, and its project axis to name this one), the runtime (a plugin's runtime as one of the core's; any,
which takes a runtime of the core, needs the axis to allow one), the model (none named on a profile that limits
models takes the first it allows), the worker it is pinned to or, unpinned, at least one worker of the dispatcher's
the profile allows, the account picked by hand, the environment (at least one of the axis's allowlist registered in
the project, ``plugin_environments.check``), and for an author run the skill create-exec-plan. A deleted profile takes
no new run (422). A worker pinned to the run whose daemon does not apply profiles, for a profile that needs one
(``profiles.needs_worker``), that could never take an environment the profile allows, or that could never build the
agent packs it attaches (``profiles.packs_unfit``), is 409, as for any worker that could never claim the run.

A run records the profile and the version it was dispatched under; the claim applies that version's rules
(``claim_clause``): a worker takes the run only when the worker axis names it or one of its labels, the runtime axis
one of its runtimes, the environment axis host or an environment of a plugin the worker takes, and, for a profile
that needs it, when its daemon says it applies profiles, and for one that attaches agent packs, when its daemon says it
builds them (``runs.AGENT_PACKS``) and its owner accepted each pack's plugin. A run asking for any runtime gets the first of the worker's
runtimes of the core that the profile allows (``pick_runtime``); a run of a plugin's runtime keeps the one it asked
for. Its environment is the first of the axis's allowlist the worker takes (``plugin_environments.pick``), the host
for an axis that allows all. A run under a profile that is not its project's default that would run on Claude Code
there goes only to a daemon that gives its agent a config dir of its own (``runs.AGENT_HOME``), and its claim says so
(``own_home``). The claimed run carries the
profile at its version (``run_profile``) for its worker, and the person who dispatched it (``run_origin``), whom the
worker names as co-author of the commits its agent authors. A run without a profile, one from before schema 0030, is
claimed as before.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import Text, and_, cast, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, JSONPATH
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, profiles, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server.profile_store import Profile, by_handle, by_id, ensure_default
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models import RunOrigin, RunProfile, available
from evo_agents.hub.server.runs.service import plugin_environments, plugin_runtimes
from evo_agents.hub.server.runs.service.worker_fit import shared_in
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.run_environments import HOST

NOTHING = "nothing was dispatched"


@dataclass(frozen=True)
class Chosen:
    """The profile a run goes under, and the model it takes."""

    profile: Profile
    model: str | None

    def columns(self) -> dict:
        return {"profile_id": self.profile.id, "profile_version": self.profile.version}

    def audit_text(self) -> str:
        """How the audit row of the dispatch names the profile: not at all for the project's default, so the row of a
        dispatch that names none reads as before profiles; `` profile=@<handle>:v<version>`` for any other."""
        return "" if self.profile.is_default else f" profile={self.profile.tag}"


@dataclass(frozen=True)
class Asked:
    """What a dispatch asks of the profile it names: the kind of run, the runtime and model, the worker it is pinned
    to (``dispatch.Pinned``), when it is, and the accounts picked by hand (``accounts.Picked``)."""

    kind: str
    runtime: str
    model: str | None
    pinned: object | None = None
    picked: tuple = ()


def _refuse(profile: Profile, why: str) -> HTTPException:
    return HTTPException(422, f"agent @{profile.handle} {why}; {NOTHING}")


async def _named(conn: AsyncConnection, user: Principal, access: ProjectAccess, handle: str) -> Profile:
    """The profile ``handle`` for a run of ``access``'s project: 404 when the caller sees none of that handle, 403
    when it belongs to another project the caller does not write to, 422 when it was deleted or its project axis
    leaves this project out."""
    profile = await by_handle(conn, handle)
    missing = HTTPException(
        404, f"no agent @{handle} that you can see: see `evo-agents hub agent list --project {access.name}`"
    )
    if profile is None:
        raise missing
    if profile.project_id != access.project_id:
        try:
            other = await project_access(conn, user, profile.project)
        except HTTPException:
            raise missing from None
        if not has_role(other.role, "writer"):
            raise HTTPException(
                403,
                f"agent @{handle} belongs to project {profile.project}, and a run under it needs the writer role "
                f"there too; you hold {other.role or 'no grant'}",
            )
        if not profiles.allows(profile.permissions, "project", access.name):
            raise _refuse(profile, f"of project {profile.project} does not take runs of project {access.name}")
    if profile.deleted_at is not None:
        raise _refuse(profile, f"was deleted at {profile.deleted_at.isoformat()} and takes no new run")
    return profile


def _model(profile: Profile, model: str | None) -> str | None:
    allow = profiles.allowed(profile.permissions, "model")
    if allow is None:
        return model
    if model is None:
        if not allow:
            raise _refuse(profile, "allows no model")
        return allow[0]
    if model not in allow:
        raise _refuse(profile, f"does not allow model {model}: it allows {', '.join(allow) or 'none'}")
    return model


def _runtime(profile: Profile, asked: Asked) -> None:
    allow = profiles.allowed(profile.permissions, "runtime")
    if allow is None:
        return
    if asked.runtime != "any" and asked.runtime not in allow:
        raise _refuse(profile, f"does not allow runtime {asked.runtime}: it allows {', '.join(allow) or 'none'}")
    if not allow:
        raise _refuse(profile, "allows no runtime")
    if asked.runtime == "any" and not any(name in runs.RUNTIMES for name in allow):
        raise _refuse(
            profile, f"allows only {', '.join(allow)}, and a run asking for any takes a runtime of the core: name one"
        )
    pinned = asked.pinned
    if pinned is not None and asked.runtime == "any":
        usable = [name for name in allow if name in runs.RUNTIMES and available(pinned.runtimes.get(name))]
        if not usable:
            raise _refuse(profile, f"allows {', '.join(allow)}, none of which worker {pinned.name} reports available")


async def _worker(conn: AsyncConnection, user: Principal, access: ProjectAccess, profile: Profile, asked: Asked):
    if not profiles.limited(profile.permissions, "worker"):
        return
    pinned = asked.pinned
    if pinned is not None:
        if not profiles.allows_worker(profile.permissions, pinned.name, pinned.labels):
            raise _refuse(profile, f"does not allow worker {pinned.name}: {_workers_text(profile)}")
        return
    w, wp = tables.workers, tables.worker_projects
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == access.project_id)
    mine_or_shared = or_(w.c.owner_id == user.user_id, shared_in(access.project_id, user.user_id))
    rows = await conn.execute(select(w.c.name, w.c.labels).where(mine_or_shared, w.c.revoked_at.is_(None), serves))
    if not any(profiles.allows_worker(profile.permissions, row.name, row.labels) for row in rows):
        raise _refuse(
            profile,
            f"allows none of your workers of project {access.name}, nor any shared there, to take the run: "
            f"{_workers_text(profile)}",
        )


def _workers_text(profile: Profile) -> str:
    return f"it allows {', '.join(profiles.allowed(profile.permissions, 'worker')) or 'no worker'}"


def _rest(profile: Profile, asked: Asked) -> None:
    """The account picked by hand, the skill of an author run, and a pinned worker that does not apply profiles or
    cannot build the profile's agent packs."""
    for picked in asked.picked:
        if not profiles.allows(profile.permissions, "credential", picked.name):
            raise _refuse(profile, f"does not allow the credential {picked.name} picked as the run's account")
    if asked.kind == "author" and not profiles.allows(profile.permissions, "skill", author.AUTHOR_SKILL):
        raise _refuse(profile, f"does not allow skill {author.AUTHOR_SKILL}, which an author run writes its plan with")
    pinned = asked.pinned
    needs = profiles.needs_worker(profile.permissions, profile.instructions)
    if pinned is not None and needs and runs.AGENT_PROFILES not in pinned.features:
        version = pinned.agent_version or "of an unknown version"
        raise HTTPException(
            409,
            f"worker {pinned.name} cannot take this run: its daemon (evo-agents {version}) does not say it applies "
            f"agent profiles, and agent @{profile.handle} limits tools, skills or models or has instructions: upgrade "
            f"it and restart its daemon; {NOTHING}",
        )
    _pinned_packs(profile, pinned)


def _pinned_packs(profile: Profile, pinned) -> None:
    """409 when the run is pinned to a worker that could never build the agent packs the profile attaches."""
    if pinned is None:
        return
    why = profiles.packs_unfit(
        profile.packs,
        worker_id=pinned.id,
        agent_version=pinned.agent_version,
        features=pinned.features,
        plugins=pinned.plugins,
    )
    if why:
        raise HTTPException(
            409,
            f"worker {pinned.name} cannot take this run of agent @{profile.handle}: {why}. Dispatch it again once the "
            f"worker's heartbeat reports that, or dispatch it to another worker; {NOTHING}",
        )


async def _environment(conn: AsyncConnection, access: ProjectAccess, profile: Profile, asked: Asked) -> None:
    """422 when the profile allows no environment the project registers; 409 when the run is pinned to a worker that
    could never take one of those it allows."""
    usable = await plugin_environments.check(conn, access.project_id, access.name, profile.handle, profile.permissions)
    pinned = asked.pinned
    if pinned is None:
        return
    why = plugin_environments.unfit(
        usable,
        worker_id=pinned.id,
        agent_version=pinned.agent_version,
        features=pinned.features,
        plugins=pinned.plugins,
    )
    if why:
        raise HTTPException(
            409,
            f"worker {pinned.name} cannot take this run: {why}. Dispatch it again once the worker's heartbeat reports "
            f"that, or dispatch it to another worker; {NOTHING}",
        )


async def choose(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, handle: str | None, asked: Asked
) -> Chosen:
    """The profile a run of ``access``'s project goes under: the one ``handle`` names, or the project's default; with
    the model it takes. HTTPException when the project does not register the runtime asked for, or the profile refuses
    what the dispatch asks."""
    await plugin_runtimes.check(conn, access.project_id, access.name, asked.runtime)
    profile = await ensure_default(conn, access.project_id) if handle is None else None
    if profile is None:
        profile = await _named(conn, user, access, handle)
    _runtime(profile, asked)
    await _environment(conn, access, profile, asked)
    model = _model(profile, asked.model)
    await _worker(conn, user, access, profile, asked)
    _rest(profile, asked)
    return Chosen(profile, model)


async def rerun_choice(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, run_id: int, asked: Asked
) -> Chosen:
    """The profile a rerun of run ``run_id`` goes under: that run's profile at its version now, checked again; the
    project's default for a run from before schema 0030."""
    r = tables.runs
    profile_id = (await conn.execute(select(r.c.profile_id).where(r.c.id == run_id))).scalar_one_or_none()
    profile = None if profile_id is None else await by_id(conn, profile_id)
    return await choose(conn, user, access, None if profile is None else profile.handle, asked)


# Claiming


def _rules():
    """The permissions of the profile version of the run being claimed, and the version's row (correlated)."""
    v = tables.agent_profile_versions
    return v, v.c.body["permissions"]


def _all(permissions, axis: str):
    """Whether ``axis`` of the version's permissions allows all; an axis the version does not name does."""
    return func.coalesce(permissions[axis]["mode"].astext, profiles.ALL) == profiles.ALL


def _packs_fit(version, features, plugins):
    """Whether a worker whose daemon says ``features`` and whose owner accepted ``plugins`` builds every agent pack of
    the profile ``version`` (correlated): one that attaches none (or a version from before packs) always."""
    packs = version.c.body["packs"]
    if runs.AGENT_PACKS not in (features or ()):
        return func.coalesce(func.jsonb_array_length(packs), 0) == 0
    path = cast(literal("$[*].plugin"), JSONPATH)
    named = func.coalesce(func.jsonb_path_query_array(packs, path), func.jsonb_build_array(), type_=JSONB)
    return named.contained_by(cast(literal(json.dumps(sorted(plugins or ()))), JSONB))


def claim_clause(
    name: str,
    labels,
    runtimes: list[str],
    features,
    plugin_runtimes: list[str] = (),
    environments: list[str] = (),
    plugins: list[str] = (),
) -> object:
    """Whether the profile of a queued run (``runs``, correlated) lets this worker take it: named by name or label on
    the worker axis, one of its ``runtimes`` of the core on the runtime axis, or for a run of a plugin's runtime one
    of its ``plugin_runtimes``, host or one of the plugins' ``environments`` it takes on the environment axis, a
    daemon that applies profiles (``features``) when the profile needs one, and one that builds agent packs when it
    attaches some, each of a plugin of ``plugins``, those its owner accepted. Always for a run without a profile."""
    r = tables.runs
    v, permissions = _rules()
    names = [name.lower(), *(profiles.LABEL_PREFIX + label.lower() for label in labels or ())]
    allow = permissions["runtime"]["allow"]
    conditions = [
        v.c.profile_id == r.c.profile_id,
        v.c.version == r.c.profile_version,
        or_(_all(permissions, "worker"), permissions["worker"]["allow"].has_any(literal(names, ARRAY(Text)))),
        or_(
            _all(permissions, "runtime"),
            allow.has_any(literal(runtimes, ARRAY(Text))),
            and_(r.c.runtime != "any", allow.has_any(literal(list(plugin_runtimes), ARRAY(Text)))),
        ),
        or_(
            _all(permissions, "environment"),
            permissions["environment"]["allow"].has_any(literal([HOST, *environments], ARRAY(Text))),
        ),
        _packs_fit(v, features, plugins),
    ]
    if runs.AGENT_PROFILES not in (features or ()):
        conditions += [_all(permissions, axis) for axis in ("tool", "skill", "model")]
        conditions.append(func.coalesce(v.c.body["instructions"].astext, "") == "")
    if runs.AGENT_HOME not in (features or ()):
        conditions.append(_default_or_not_claude(permissions, runtimes))
    return or_(r.c.profile_id.is_(None), exists().where(*conditions))


CLAUDE = "claude-code"


def _default_or_not_claude(permissions, runtimes: list[str]):
    """Whether a worker whose daemon gives no agent a config dir of its own (``runs.AGENT_HOME``) may take the run
    being claimed (correlated): one under its project's default profile, or one that does not run on Claude Code here,
    neither asking for it nor, asking for any, getting it from ``pick_runtime`` among the worker's ``runtimes``."""
    r, ap = tables.runs, tables.agent_profiles
    default = select(ap.c.is_default).where(ap.c.id == r.c.profile_id).correlate(r).scalar_subquery()
    allow = permissions["runtime"]["allow"]
    if CLAUDE not in runtimes:
        picks_claude = literal(False)
    else:
        before = runtimes[: runtimes.index(CLAUDE)]
        allows_claude = or_(_all(permissions, "runtime"), allow.has_any(literal([CLAUDE], ARRAY(Text))))
        picks_claude = (
            allows_claude
            if not before
            else and_(
                ~_all(permissions, "runtime"),
                allow.has_any(literal([CLAUDE], ARRAY(Text))),
                ~allow.has_any(literal(before, ARRAY(Text))),
            )
        )
    return or_(
        func.coalesce(default, False),
        and_(r.c.runtime != CLAUDE, or_(r.c.runtime != "any", ~picks_claude)),
    )


def runtime_rule(axis: str = "runtime"):
    """``axis`` (the runtime axis unless named) of the profile version of the run being claimed (correlated); NULL
    without a profile, or for an axis the version does not name."""
    r = tables.runs
    v, permissions = _rules()
    return (
        select(permissions[axis])
        .where(v.c.profile_id == r.c.profile_id, v.c.version == r.c.profile_version)
        .scalar_subquery()
    )


def run_packs():
    """The agent packs of the profile version of the run being claimed (correlated), as the version stores them; NULL
    without a profile, or for a version from before packs."""
    r, v = tables.runs, tables.agent_profile_versions
    return (
        select(v.c.body["packs"])
        .where(v.c.profile_id == r.c.profile_id, v.c.version == r.c.profile_version)
        .scalar_subquery()
    )


def pick_runtime(asked: str, runtimes: list[str], rule: dict | None) -> str:
    """The runtime of a claimed run: the one it asked for, or for ``any`` the first of ``runtimes``, the worker's of the
    core, the profile's runtime axis ``rule`` allows (``claim_clause`` made sure there is one)."""
    if asked != "any":
        return asked
    allow = profiles.allowed({"runtime": rule}, "runtime") if rule else None
    return next(name for name in runtimes if allow is None or name in allow)


async def own_home(conn: AsyncConnection, run_id: int) -> bool:
    """Whether run ``run_id`` went under a profile that is not its project's default, whose claude-code agent gets a
    config dir of its own (``runs.AGENT_HOME``); False for a run without a profile."""
    r, ap = tables.runs, tables.agent_profiles
    query = select(ap.c.is_default).join_from(r, ap, ap.c.id == r.c.profile_id).where(r.c.id == run_id)
    default = (await conn.execute(query)).scalar_one_or_none()
    return default is False


async def run_origin(conn: AsyncConnection, run_id: int) -> RunOrigin:
    """The person who dispatched run ``run_id``, with their GitHub login and id, whom its worker names as co-author of
    the commits its agent authors."""
    r, u = tables.runs, tables.users
    query = select(u.c.login, u.c.github_id).join_from(r, u, u.c.id == r.c.dispatched_by).where(r.c.id == run_id)
    row = (await conn.execute(query)).one()
    return RunOrigin(login=row.login, github_id=row.github_id)


async def run_profile(conn: AsyncConnection, run_id: int) -> RunProfile | None:
    """The profile of run ``run_id`` at the version the run went under; None for a run without one."""
    r, v = tables.runs, tables.agent_profile_versions
    query = (
        select(v.c.version, v.c.body)
        .join_from(r, v, (v.c.profile_id == r.c.profile_id) & (v.c.version == r.c.profile_version))
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    body = row.body
    return RunProfile(
        handle=body["handle"],
        version=row.version,
        name=body["name"],
        instructions=body.get("instructions") or "",
        permissions=body["permissions"],
    )
