"""The principal of a run and the snapshot of its autonomy (plan autonomy-levels, criteria a4, a5 and a7).

Principal: the person a run acts for, whose effective autonomy (``autonomy_levels.effective`` over the sources
``org.autonomy_sources`` lists) bounds it. ``stamp`` sets it, with the snapshot, on every run ``insert_run`` queues:

- a step, plan, author or task run: the member who dispatched it (a task run's dispatcher is its agent type's run_as);
- a chat run: the person who wrote the message that mentions the agent (``chat_runs``); for an agent type with a
  run_as, also run_as, the run bounded by both (``For.also``), so an intern who mentions an agent run as their lead
  still has the intern's modes; for a message of an agent, the principal of that agent's run, within its snapshot
  (``For.within``); for a message of the hub (a triage thread), as for a run the hub makes;
- a run the hub makes on its own (``HUB_KINDS``: the night shift's, the release cycle's, and a triage chat's): the
  sponsor of the live seat of the run's agent profile, and the dispatcher as before when the profile holds none; the
  Loop's runs act for the owner of the work item's goal (``loop.dispatch``).

A next attempt and a resumed run copy both from the run before them; a local run has neither.

Snapshot: runs.autonomy (``autonomy_levels.snapshot``) as the hub computed it when it queued the run, over the run's
repos (each repo's project policy, the strictest of them) and its agent profile. Every gate takes the stricter of the
snapshot and the value now (``recheck``): a lower level reaches a running run at its next gate, a higher one only the
runs queued after it. A recheck that comes out stricter is written back, so the snapshot only tightens.

Claim (criterion a7): a worker takes a run only when its name is among the workers the snapshot allows and the host or
an environment it takes among its environments (``claim_clause``), and a run with a class below auto
(``autonomy_levels.restricted``) only on a daemon that says ``runs.AUTONOMY``, on claude-code. ``claim_fits`` asks the
same of the recheck at the claim. A run without a snapshot is claimed as before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import Text, and_, false, func, literal, or_, select, true, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import autonomy, autonomy_levels, org, profiles, runs, tables
from evo_agents.hub.autonomy_levels import Reached
from evo_agents.plugins.run_environments import HOST

HUB_KINDS = (*runs.CURATOR_KINDS, "release")  # the kinds of run the hub makes on its own


@dataclass(frozen=True)
class For:
    """Who a run acts for, when the kind alone does not say: the principal (None: by the kind), the persons whose
    autonomy bounds it as well (an agent type's run_as), a run whose snapshot it stays within (the run of the agent
    that asked for it), whether the hub made it on its own (its agent's sponsor acts), and repos it will name."""

    user_id: int | None = None
    also: tuple[int, ...] = ()
    within: int | None = None
    hub: bool = False
    repos: tuple[str, ...] = ()  # repos the run will name, set after its insert (a chat run's)


@dataclass(frozen=True)
class Context:
    """What every effective autonomy of one moment reads: the org chart and the day."""

    chart: org.Chart
    day: date


async def context(conn: AsyncConnection) -> Context:
    from evo_agents.hub.server.org_store import load, today

    return Context(await load(conn), await today(conn))


def sponsor(chart: org.Chart, profile_id: int | None, day: date) -> int | None:
    """The sponsor of the live seat of agent profile ``profile_id``; None when it holds none."""
    for seat in chart.seats.values():
        if seat.profile_id is not None and seat.profile_id == profile_id and not chart.ended(seat, day):
            return seat.sponsor_id
    return None


def run_repos(repo: str | None, repos) -> list[str]:
    """The repos a run names: its repo, and those of its list of repos."""
    names = [repo] if repo else []
    if isinstance(repos, list):
        names += [entry["repo"] for entry in repos if isinstance(entry, Mapping) and entry.get("repo")]
    return list(dict.fromkeys(names))


async def _login(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def _policies(conn: AsyncConnection, project_id: int, repos: list[str]) -> list[autonomy_levels.Source]:
    """Source 4 for each of ``repos`` the project lists; a repo it does not list has none."""
    from evo_agents.hub.server.autonomy_policies import effective_policy

    if not repos:
        return []
    p = tables.projects
    project = (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()
    held = (await effective_policy(conn, project_id))[autonomy.REPOS]
    return [
        autonomy_levels.policy_source(project, name, held[name]["mode"], held[name]["prod_release"])
        for name in repos
        if name in held
    ]


async def _profile(conn: AsyncConnection, profile_id: int | None) -> tuple[str, int] | None:
    if profile_id is None:
        return None
    ap = tables.agent_profiles
    handle = (await conn.execute(select(ap.c.handle).where(ap.c.id == profile_id))).scalar_one_or_none()
    return None if handle is None else (handle, profile_id)


async def reached(
    conn: AsyncConnection, at: Context, user_id: int, project_id: int, repos: list[str], profile_id: int | None
) -> Reached:
    """The effective autonomy of ``user_id`` running the agent profile ``profile_id`` on ``repos`` of the project now:
    the strictest over the repos, each under its project policy."""
    from evo_agents.hub.server.autonomy_store import newest_pref

    login = await _login(conn, user_id)
    pref = await newest_pref(conn, user_id)
    held = autonomy_levels.pref_source(login, *((None,) if pref is None else (pref.level, pref.overrides)))
    profile = await _profile(conn, profile_id)
    found = None
    for policy in await _policies(conn, project_id, repos) or [None]:
        sources = org.autonomy_sources(at.chart, user_id, at.day, held, policy=policy, profile=profile)
        one = autonomy_levels.effective(sources)
        found = one if found is None else autonomy_levels.stricter(found, one)
    return found


async def _bounded(
    conn: AsyncConnection, at: Context, people: Iterable[int], project_id: int, repos: list[str], profile_id
) -> Reached:
    found = None
    for user_id in people:
        one = await reached(conn, at, user_id, project_id, repos, profile_id)
        found = one if found is None else autonomy_levels.stricter(found, one)
    return found


def _lowered(found: Reached) -> Reached:
    """``found`` with the worker names in lower case, as a claim compares them."""
    if found.workers is None:
        return found
    return Reached(
        found.level,
        found.level_source,
        found.classes,
        found.environments,
        tuple(sorted({name.lower() for name in found.workers})),
    )


def body(found: Reached, also: Iterable[str] = (), taken_at: str | None = None) -> dict:
    """The JSON runs.autonomy holds for ``found``, with whether it is ``restricted``."""
    found = _lowered(found)
    taken = taken_at or datetime.now(UTC).isoformat()
    return {**autonomy_levels.snapshot(found, taken, also), "restricted": autonomy_levels.restricted(found)}


async def of_message(conn: AsyncConnection, message_id: int, run_as: int | None, origin: int, repos=()) -> For:
    """Who a chat run woken by message ``message_id`` acts for: its writer, or for an agent's message the principal of
    the agent's run, within that run's snapshot (``origin``, the person at the origin of the chain, when the run is
    gone), or for a message of the hub its agent's sponsor; bounded by ``run_as`` too, the person an agent type runs
    as."""
    m = tables.chat_messages
    query = select(m.c.author_user_id, m.c.author_profile_id, m.c.run_id).where(m.c.id == message_id)
    row = (await conn.execute(query)).one_or_none()
    also = () if run_as is None else (run_as,)
    if row is not None and row.author_user_id is not None:
        return For(user_id=row.author_user_id, also=also, repos=tuple(repos))
    if row is not None and row.run_id is not None:
        return For(within=row.run_id, also=also, repos=tuple(repos))
    if row is not None and row.author_profile_id is None:
        return For(also=also, hub=True, repos=tuple(repos))
    return For(user_id=origin, also=also, repos=tuple(repos))


async def _parent(conn: AsyncConnection, run_id: int):
    r = tables.runs
    return (await conn.execute(select(r.c.principal_id, r.c.autonomy).where(r.c.id == run_id))).one_or_none()


async def stamp(conn: AsyncConnection, values: dict, who: For | None = None) -> dict:
    """``values`` of a run being queued with its principal_id and its autonomy snapshot (see the module); values that
    carry a principal already are left as they are."""
    if "principal_id" in values:
        return values
    who = who or For()
    at = await context(conn)
    parent = None if who.within is None else await _parent(conn, who.within)
    principal = who.user_id
    if principal is None and parent is not None:
        principal = parent.principal_id
    if principal is None and (who.hub or values.get("kind") in HUB_KINDS):
        principal = sponsor(at.chart, values.get("profile_id"), at.day)
    if principal is None:
        principal = values["dispatched_by"]
    people = list(dict.fromkeys([principal, *who.also]))
    repos = list(dict.fromkeys([*run_repos(values.get("repo"), values.get("repos")), *who.repos]))
    found = await _bounded(conn, at, people, values["project_id"], repos, values.get("profile_id"))
    if parent is not None and parent.autonomy is not None:
        found = autonomy_levels.stricter(found, autonomy_levels.from_snapshot(parent.autonomy))
    also = [await _login(conn, user_id) for user_id in people[1:]]
    return {**values, "principal_id": principal, "autonomy": body(found, also)}


async def recheck(conn: AsyncConnection, run_id: int, *, repo: str | None = None) -> Reached | None:
    """The autonomy of run ``run_id`` at a gate: the stricter of its snapshot and the value now, on ``repo`` when the
    gate acts on one (the run's repos without it), written back when stricter than the snapshot. None for a run
    without a snapshot (a local run, one queued before schema 0106): the gate acts as before."""
    from evo_agents.hub.server.org_store import user_by_login

    r = tables.runs
    columns = (r.c.principal_id, r.c.autonomy, r.c.project_id, r.c.repo, r.c.repos, r.c.profile_id)
    row = (await conn.execute(select(*columns).where(r.c.id == run_id))).one_or_none()
    if row is None or row.autonomy is None or row.principal_id is None:
        return None
    people = [row.principal_id]
    for login in row.autonomy.get("also") or ():
        found = await user_by_login(conn, login)
        if found is not None:
            people.append(found.id)
    repos = [repo] if repo else run_repos(row.repo, row.repos)
    now = await _bounded(conn, await context(conn), people, row.project_id, repos, row.profile_id)
    held = autonomy_levels.from_snapshot(row.autonomy)
    merged = _lowered(autonomy_levels.stricter(held, now))
    if merged != held:
        written = {**body(merged, row.autonomy.get("also") or (), row.autonomy["taken_at"])}
        written["tightened_at"] = datetime.now(UTC).isoformat()
        await conn.execute(update(r).where(r.c.id == run_id).values(autonomy=written))
    return merged


# The claim (criterion a7)


def runtime_fits(asked: str, runtimes: Iterable[str], rule: Mapping | None) -> bool:
    """Whether a run that asked for ``asked`` runs on claude-code on a worker of ``runtimes`` (of the core), under the
    profile's runtime axis ``rule``."""
    if asked == runs.AUTONOMY_RUNTIME:
        return True
    if asked != "any" or runs.AUTONOMY_RUNTIME not in runtimes:
        return False
    return rule is None or profiles.allows({"runtime": rule}, "runtime", runs.AUTONOMY_RUNTIME)


def claim_fits(found: Reached, worker: str, environments: Iterable[str], features, runtime_ok: bool) -> str | None:
    """Why a worker named ``worker``, taking ``environments`` besides the host, whose daemon says ``features``, may not
    take a run of autonomy ``found``; None when it may. ``runtime_ok``: the run can run on claude-code there."""
    if found.workers is not None and worker.lower() not in found.workers:
        return f"its autonomy allows the workers {', '.join(found.workers) or 'none'}"
    if found.environments is not None and not {HOST, *environments} & set(found.environments):
        return f"its autonomy allows the environments {', '.join(found.environments) or 'none'}"
    if autonomy_levels.restricted(found) and not (runs.AUTONOMY in (features or ()) and runtime_ok):
        return "a class of its autonomy is below auto: it runs on claude-code under a daemon that applies the modes"
    return None


def environment_rule(rule: Mapping | None, found: Reached | None) -> Mapping | None:
    """The environment axis ``rule`` of a run's profile narrowed to the environments its autonomy ``found`` allows,
    host first among them for an axis that allows all."""
    if found is None or found.environments is None:
        return rule
    allow = profiles.allowed({"environment": rule} if rule else None, "environment")
    if allow is None:
        allow = sorted(found.environments, key=lambda name: name != HOST)
    return {"mode": profiles.ALLOWLIST, "allow": [name for name in allow if name in found.environments]}


def claim_runtime(found: Reached | None) -> str | None:
    """The runtime a run of autonomy ``found`` takes at its claim: claude-code for one with a class below auto, the
    one the run asked for (None) otherwise."""
    return runs.AUTONOMY_RUNTIME if found is not None and autonomy_levels.restricted(found) else None


def _claude_allowed(rule):
    """Whether the runtime axis ``rule`` (a correlated subquery, NULL without a profile) allows claude-code."""
    mode = func.coalesce(rule["mode"].astext, profiles.ALL)
    return or_(mode == profiles.ALL, func.coalesce(rule["allow"].has_key(runs.AUTONOMY_RUNTIME), false()))


def claim_clause(name: str, runtimes: Iterable[str], features, environments: Iterable[str], rule) -> object:
    """Whether a queued run (``runs``, correlated) may go to this worker by its autonomy snapshot (see
    ``claim_fits``); ``rule`` is the profile's runtime axis of the run (``profiles.runtime_rule``)."""
    r = tables.runs
    snap = r.c.autonomy
    places = func.coalesce(func.jsonb_typeof(snap["workers"]), "null") != "array"
    worker = or_(places, snap["workers"].has_key(name.lower()))
    kinds = func.coalesce(func.jsonb_typeof(snap["environments"]), "null") != "array"
    environment = or_(kinds, snap["environments"].has_any(literal([HOST, *environments], ARRAY(Text))))
    if runs.AUTONOMY in (features or ()):
        claude = runs.AUTONOMY_RUNTIME in set(runtimes)
        runtime = or_(
            r.c.runtime == runs.AUTONOMY_RUNTIME,
            and_(r.c.runtime == "any", true() if claude else false(), _claude_allowed(rule)),
        )
    else:
        runtime = false()
    held = or_(~func.coalesce(snap["restricted"].as_boolean(), false()), runtime)
    return or_(snap.is_(None), and_(worker, environment, held))
