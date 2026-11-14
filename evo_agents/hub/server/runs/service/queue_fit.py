"""Why a queued run waits (GET /v1/projects/{p}/runs/{id}/rerun-candidates of a queued run, the inbox's diagnosis of
it, and the warning a dispatch answers with): each worker that could take it, with whether it can now and why not, the
agent pools of the project, and, when no worker can, one line that says why with the command that fixes it.

The hub reads each worker as its claim does (``claim_rules.read_claimer``) and checks the run against each condition of
that claim on its own (``Claimer.stops`` and ``Claimer.conditions``, the parts of ``_dispatcher_clause`` for whose
runs it takes), so what it says is what the claim does, with no second set of rules to drift from it. The workers it
reads are those of the run's owner and those shared in the run's project, the worker the run is pinned to, or, for a
run of an agent in pool mode, the nodes of the agent's pool (``agent_placement``), whoever owns them.

A worker fits when nothing but a wait keeps it from the run: no free slot, a run of another member on a shared worker,
a lost attempt of the run while it is not steady yet, another run of the plan editing the same files, too little
free disk on its worktrees' drive (its daemon frees room), no free browser, or Chromium the hub asked it to install
less than ``browser_install.UNFINISHED_AFTER`` ago. Anything else,
an offline worker included, keeps it from the run until someone acts; a worker of the run's owner that lacks only
Chromium gets the hub's request to install it on the reaper's next pass (``browser_install.ask_for_queued``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import ops_events, runs, tables
from evo_agents.hub.server import agent_placement, browser_install, kg_shared_pool, worker_placement
from evo_agents.hub.server.profile_store import profile_of, profile_select
from evo_agents.hub.server.runs.service import claim_rules, claims, overlap
from evo_agents.hub.server.runs.service.candidates import PoolCandidate, RerunCandidates, WorkerCandidate
from evo_agents.hub.server.runs.service.worker_fit import OFFLINE, worker_problems


@dataclass
class Verdict:
    """One worker for a queued run: what keeps it from the run until someone acts (``blocking``, with the key of each
    in ``blocked_by``), and what it waits for (``waiting``)."""

    row: object
    claimer: claim_rules.Claimer
    blocking: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)

    @property
    def fit(self) -> bool:
        return not self.blocking


@dataclass(frozen=True)
class QueueFit:
    """Who can take a queued run: the run's row, a ``Verdict`` for each worker read, the run's agent pool when its agent
    is in pool mode, and why no worker can (None when one can)."""

    run: object
    verdicts: list[Verdict]
    home: agent_placement.Home | None
    diagnosis: str | None


async def _run(conn: AsyncConnection, run_id: int):
    r, p, u = tables.runs, tables.projects, tables.users
    query = (
        select(
            r.c.id,
            r.c.project_id,
            p.c.name.label("project"),
            r.c.dispatched_by,
            u.c.login.label("owner"),
            r.c.dispatched_via,
            r.c.kind,
            r.c.profile_id,
            r.c.pinned_worker_id,
            r.c.selector,
            r.c.tolerations,
            r.c.requested_runtime,
            r.c.repo,
            r.c.repos,
            r.c.browser,
        )
        .join_from(r, p, p.c.id == r.c.project_id)
        .join(u, u.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one()


def _workers(run, home: agent_placement.Home | None):
    """The workers that could take ``run``, not revoked: the nodes of the agent's pool ``home`` when its agent is in pool
    mode (and of the shared KG pool for a run of its kinds), else the owner's and those shared in its project; and the
    one it is pinned to. The owner's first."""
    w, wp, r, u = tables.workers, tables.worker_projects, tables.runs, tables.users
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == run.project_id)
    held = select(func.count()).where(r.c.worker_id == w.c.id, r.c.state.in_(runs.HELD_STATES)).scalar_subquery()
    owner = select(u.c.login).where(u.c.id == w.c.owner_id).scalar_subquery()
    if home is not None:
        kg_pool = tables.worker_pools
        shared = select(kg_pool.c.id).where(kg_pool.c.kg_shared)  # its KG runs may go there (``kg_shared_pool``)
        who = or_(w.c.pool_id == home.pool_id, and_(literal(run.kind in kg_shared_pool.KINDS), w.c.pool_id.in_(shared)))
    else:
        who = or_(w.c.owner_id == run.dispatched_by, and_(w.c.shared, serves))
    if run.pinned_worker_id is not None:
        who = or_(who, w.c.id == run.pinned_worker_id)
    return (
        select(
            *claims.worker_columns(w),
            owner.label("owner"),
            serves.label("serves"),
            (w.c.last_heartbeat_at >= func.now() - OFFLINE).label("online"),
            w.c.last_heartbeat_at,
            held.label("holding"),
            literal(0).label("others"),
            *worker_placement.columns(w),
            *browser_install.columns(),
        )
        .where(w.c.revoked_at.is_(None), who)
        .order_by(w.c.owner_id != run.dispatched_by, w.c.id)
    )


async def _failed(conn: AsyncConnection, run_id: int, conditions: list[claim_rules.Condition]) -> list[str]:
    """The keys of ``conditions`` that run ``run_id`` does not meet, read on ``conn`` with no lock."""
    if not conditions:
        return []
    r = tables.runs
    columns = [clause.label(f"c{index}") for index, (_, clause) in enumerate(conditions)]
    values = (await conn.execute(select(*columns).select_from(r).where(r.c.id == run_id))).one()
    return [key for (key, _), value in zip(conditions, values, strict=True) if not value]


def _ago(at: datetime | None) -> str:
    if at is None:
        return "it has sent no heartbeat"
    minutes = int((datetime.now(UTC) - at).total_seconds() // 60)
    return f"its last heartbeat was {minutes} min ago"


def _owners_command(row, run, command: str) -> str:
    """``command`` to run, as the run's owner when the worker is theirs, else as the worker's owner."""
    return f"`{command}`" if row.owner_id == run.dispatched_by else f"its owner {row.owner} runs `{command}`"


def _stop_reasons(key: str, row, run, claimer: claim_rules.Claimer) -> tuple[str, bool]:
    """(why, whether it waits) for the stop ``key`` of ``Claimer.stops``."""
    version = _version(row)
    if key == "unready":
        return f"is not ready: {claimer.node.unready}", False
    if key == "full":
        return f"has no free slot ({claimer.holding} of {row.slots} held): it takes the run once one frees", True
    if key == "runtime":
        return "reports no runtime available: install one on its machine (claude-code, codex, ...)", False
    if key == "disk":
        free = ((row.health or {}).get("disk") or {}).get("free_gb")
        reserve = ops_events.disk_reserve(row.health)
        again = reserve + ops_events.disk_headroom(row.health)
        return (
            f"has {free} GB free on its worktrees' drive, under its reserve of {reserve:g} GB or not yet back over "
            f"{again:g} GB since: it takes runs again once its daemon removed the worktrees of ended runs or its "
            "owner freed room",
            True,
        )
    return (
        f"has no checkout of a project it serves, and its daemon (evo-agents {version}) does not clone what it lacks: "
        "update its daemon",
        False,
    )


async def _dispatcher_reasons(conn: AsyncConnection, row, run) -> list[tuple[str, bool]]:
    """Why the worker takes none of the runs of the run's owner: the parts of ``_dispatcher_clause`` the run fails."""
    if not row.shared:
        return [(f"takes only the runs of its owner {row.owner}, and is shared with nobody", False)]
    parts = claim_rules.dispatcher_parts(row.id, row.owner_id, row.shared)
    failed = set(await _failed(conn, run.id, list(parts.items())))
    found = []
    if "own" in failed:
        if "writer" in failed:
            found.append((f"is shared, but {run.owner} holds no writer role on project {run.project}", False))
        if "unit" in failed:
            found.append(
                (
                    f"is shared only with units of the org chart {run.owner} is not seated in, or its newest doctor "
                    "report has a high finding",
                    False,
                )
            )
        if "not_night" in failed:
            found.append(
                (f"is shared, and a {run.kind} run of the night shift runs only on its owner's workers", False)
            )
    if "alone" in failed:
        found.append(("holds a run of another member, and a shared worker runs one member's runs at a time", True))
    return found


def _project_reason(row, run, profile, home) -> tuple[str, bool]:
    project = run.project
    if not row.serves:
        command = f"evo-agents worker projects --worker {row.name} --add {project}"
        return f"does not serve project {project}: {_owners_command(row, run, command)}", False
    return (
        f"serves project {project}, but its owner {row.owner} holds no writer role there, and a worker takes only "
        "the runs of projects its owner writes in",
        False,
    )


def _pinned_reason(row, run, profile, home) -> tuple[str, bool]:
    return f"the run is pinned to worker #{run.pinned_worker_id}", False


def _runtime_reason(row, run, profile, home) -> tuple[str, bool]:
    asked = run.requested_runtime or "any"
    if asked == "any":
        return "reports no runtime of the core available (claude-code, codex, ...)", False
    return f"does not report {asked} available, which the run asks for", False


def _checkout_reason(row, run, profile, home) -> tuple[str, bool]:
    repos = [entry.get("repo") for entry in run.repos or () if isinstance(entry, dict)] or [run.repo]
    names = ", ".join(f"{run.project}/{repo}" for repo in repos if repo)
    return (
        f"has no checkout of {names or 'the run repo'}, and its daemon (evo-agents {_version(row)}) does not clone "
        "what it lacks: update its daemon",
        False,
    )


def _profile_reason(row, run, profile, home) -> tuple[str, bool]:
    handle = getattr(profile, "handle", None)
    problems = []
    if profile is not None:
        via = run.dispatched_via or "machine"
        problems = worker_problems(row, profile, via, "chat", run.requested_runtime or "any")
    detail = [why for why in problems if (handle and f"@{handle}" in why) or "agent profiles" in why]
    said = f": it {'; it '.join(detail)}" if detail else ""
    return (
        f"is not one the run's agent profile{f' @{handle}' if handle else ''} or its autonomy lets take it{said}",
        False,
    )


def _minutes_since(at: datetime) -> int:
    return int((datetime.now(UTC) - at).total_seconds() // 60)


def _browser_reason(row, run, profile, home) -> tuple[str, bool]:
    features = row.features or ()
    if runs.BROWSER in features:
        return "has no free browser, or another run holds the run's browser identity: it takes the run then", True
    lacks = "starts no browser, and the run asks for a Chromium of its own"
    install = _owners_command(row, run, "evo-agents worker browser install")
    if browser_install.is_open(row):
        minutes = _minutes_since(row.browser_install_requested_at)
        if datetime.now(UTC) - row.browser_install_requested_at < browser_install.UNFINISHED_AFTER:
            return f"{lacks}: it installs the Chromium asked {minutes} min ago, and takes the run then", True
        return f"{lacks}: the Chromium asked {minutes} min ago is not installed yet; on its machine, {install}", False
    if runs.BROWSER_INSTALL not in features:
        return f"{lacks}: on its machine, {install}, or update its daemon so the hub installs one", False
    if browser_install.held_back(row):
        how = row.browser_install_outcome
        why = f" ({row.browser_install_reason})" if row.browser_install_reason else ""
        return f"{lacks}, and its last install from the hub ended {how}{why}: on its machine, {install}", False
    if row.owner_id != run.dispatched_by:
        return (
            f"{lacks}: its owner {row.owner} installs one with Install browser, or runs `evo-agents worker browser install`",
            False,
        )
    return f"{lacks}: the hub asks it to install one within a minute", False


def _agent_pool_reason(row, run, profile, home) -> tuple[str, bool]:
    if home is not None:
        return f"is not a node of pool {home.pool}, where the runs of @{home.handle} go", False
    return "is a node of the pool of another agent, which takes that agent's runs alone", False


def _pool_accounts_reason(row, run, profile, home) -> tuple[str, bool]:
    handle = getattr(profile, "handle", None)
    return (
        f"the account pool of @{handle or 'the agent'} has no account the run can use now: add one, or wait "
        "for one to cool down",
        False,
    )


def _placement_reason(row, run, profile, home) -> tuple[str, bool]:
    node = worker_placement.Placement.of(row)
    pinned = run.pinned_worker_id == row.id
    problems = node.problems(run.selector or (), run.tolerations or (), pinned=pinned, readiness=False)
    return "; ".join(problems) or "its labels or taints do not fit the run", False


def _version(row) -> str:
    return row.agent_version or "of an unknown version"


_FIXED_REASONS = {  # the conditions whose reason reads nothing of the worker or the run
    "draining": (
        "is draining, and takes only the runs that resume one it parked: on its machine, "
        "`evo-agents worker drain --resume`",
        False,
    ),
    "web_only": ("takes only runs dispatched from the web, as its owner set it, and this one was not", False),
    "task_worker": ("is not one of the workers the run's agent type names", False),
    "lost_here": ("lost an earlier attempt of the run and is not steady yet: it takes the run once it is", True),
}
_DAEMON_REASONS = {  # the conditions a newer daemon passes: {version} is the worker's, {kind} the run's
    "plan_runs": f"runs evo-agents {{version}}, and a plan run needs {runs.version_text(runs.PLAN_RUN_AGENT)} or "
    "later: update its daemon",
    "step_sessions": "its daemon (evo-agents {version}) runs no plan run with a session per step: update it",
    "kinds": "its daemon (evo-agents {version}) does not say it runs {kind} runs: update it",
    "gateway": "runs evo-agents {version}, which takes no run of the model gateway: update its daemon",
    "merge_judge": "its daemon (evo-agents {version}) does not say it judges merge requests: update it and restart it",
    "repoless_review": "its daemon (evo-agents {version}) runs no review with no repo: update it and restart it",
    "probe_change_arms": "its daemon (evo-agents {version}) does not build a probe's arms at the change's commits: "
    "update it and restart it",
}
_REASONS = {
    "project": _project_reason,
    "pinned": _pinned_reason,
    "runtime": _runtime_reason,
    "checkout": _checkout_reason,
    "profile": _profile_reason,
    "browser": _browser_reason,
    "agent_pool": _agent_pool_reason,
    "pool_accounts": _pool_accounts_reason,
    "placement": _placement_reason,
}


def _condition_reason(key: str, row, run, profile, home) -> tuple[str, bool]:
    """(why, whether it waits) for the condition ``key`` of ``Claimer.conditions`` the run fails on worker ``row``."""
    if key in _FIXED_REASONS:
        return _FIXED_REASONS[key]
    if key in _DAEMON_REASONS:
        return _DAEMON_REASONS[key].format(version=_version(row), kind=run.kind), False
    reason = _REASONS.get(key)
    if reason is None:
        return f"fails the claim's condition {key}", False
    return reason(row, run, profile, home)


async def _verdict(conn: AsyncConnection, run, row, profile, home) -> Verdict:
    """What keeps worker ``row`` from the queued ``run`` now, as its claim counts it."""
    claimer = await claim_rules.read_claimer(conn, row, worker_placement.Placement.of(row))
    verdict = Verdict(row=row, claimer=claimer)

    def add(key: str, reason: tuple[str, bool]) -> None:
        if reason[1]:
            verdict.waiting.append(reason[0])
        else:
            verdict.blocking.append(reason[0])
            verdict.blocked_by.append(key)

    if not row.online:
        add(
            "offline",
            (f"is offline: {_ago(row.last_heartbeat_at)}; start its daemon (`evo-agents worker service`)", False),
        )
    elsewhere = run.project_id not in claimer.served  # the project it takes no run of says why: no checkout follows
    for key in claimer.stops():
        if not (key == "checkout" and elsewhere):
            add(key, _stop_reasons(key, row, run, claimer))
    failed = await _failed(conn, run.id, claimer.conditions())
    for key in failed:
        if key == "queued" or (key == "checkout" and elsewhere):
            continue
        if key == "dispatcher":
            for reason in await _dispatcher_reasons(conn, row, run):
                add(key, reason)
            continue
        add(key, _condition_reason(key, row, run, profile, home))
    if not failed and run.id in await overlap.blocked(conn, list(claimer.served)):
        add("overlap", ("waits for another run of the plan that edits the files the step names", True))
    return verdict


def _distance(verdict: Verdict) -> tuple:
    """How far a worker is from taking the run: serving its project, online, the fewest reasons it cannot, then the
    most free slots and the smallest id; the nearest names the command a diagnosis gives."""
    row = verdict.row
    return (not row.serves, not row.online, len(verdict.blocking), verdict.claimer.holding - row.slots, row.id)


def nearest(verdicts: list[Verdict]) -> Verdict:
    """The worker of ``verdicts`` nearest to taking the run (``_distance``)."""
    return min(verdicts, key=_distance)


def _diagnosis(run, verdicts: list[Verdict], home) -> str | None:
    """Why no worker can take ``run``, with the command that fixes it; None when one can."""
    if any(verdict.fit for verdict in verdicts):
        return None
    project = run.project
    if home is not None and not verdicts:
        return (
            f"agent @{home.handle} runs in pool mode, and its pool {home.pool} has no node: create a join token with "
            f"`evo-agents worker pool token create {home.pool}` and join a machine with it (`evo-agents worker join`)"
        )
    if not verdicts:
        return (
            f"{run.owner} has no worker, and none is shared in project {project}: join a machine with `evo-agents "
            f"worker join`, then `evo-agents worker projects --add {project}` on it"
        )
    if not any(verdict.row.serves for verdict in verdicts):
        own = [v for v in verdicts if v.row.owner_id == run.dispatched_by] or verdicts
        closest = nearest(own)
        command = f"evo-agents worker projects --worker {closest.row.name} --add {project}"
        return f"no worker serves project {project}: {_owners_command(closest.row, run, command)}"
    closest = nearest(verdicts)
    where = f"no node of pool {home.pool} can take it" if home is not None else "no worker can take it"
    return f"{where}; the nearest, {closest.row.name}, {'; '.join(closest.blocking)}"


async def _profile(conn: AsyncConnection, run):
    ap = tables.agent_profiles
    if run.profile_id is None:
        return None
    row = (await conn.execute(profile_select().where(ap.c.id == run.profile_id))).one_or_none()
    return None if row is None else profile_of(row)


async def queue_fit(conn: AsyncConnection, run_id: int) -> QueueFit:
    """Who can take the queued run ``run_id`` now, read on ``conn`` (the dispatch's own transaction, too)."""
    run = await _run(conn, run_id)
    home = await agent_placement.home_of(conn, run.profile_id)
    profile = await _profile(conn, run)
    rows = (await conn.execute(_workers(run, home))).all()
    verdicts = [await _verdict(conn, run, row, profile, home) for row in rows]
    return QueueFit(run=run, verdicts=verdicts, home=home, diagnosis=_diagnosis(run, verdicts, home))


async def warning(conn: AsyncConnection, run_id: int) -> str | None:
    """What a dispatch says of the run ``run_id`` it just queued: why no worker can take it, None when one can."""
    found = await queue_fit(conn, run_id)
    if found.diagnosis is None:
        return None
    return f"run #{run_id} is queued, but {found.diagnosis}"


async def _pools(conn: AsyncConnection, project_id: int, home, verdicts: list[Verdict]) -> list[PoolCandidate]:
    """The pools of the agents of the project, each with its nodes, whether the run goes there and why."""
    wp, ap, w = tables.worker_pools, tables.agent_profiles, tables.workers
    nodes = select(func.count()).where(w.c.pool_id == wp.c.id, w.c.revoked_at.is_(None)).scalar_subquery()
    query = (
        select(
            wp.c.id,
            wp.c.name,
            wp.c.taints,
            ap.c.id.label("agent_id"),
            ap.c.handle,
            nodes.label("nodes"),
        )
        .join_from(wp, ap, ap.c.id == wp.c.agent_id)
        .where(ap.c.project_id == project_id, ap.c.deleted_at.is_(None))
        .order_by(wp.c.name)
    )
    found = []
    for pool in await conn.execute(query):
        place = agent_placement.Home(
            pool.id, pool.name, tuple(pool.taints or ()), pool.agent_id, pool.handle, project_id
        )
        ready = len(await agent_placement.ready_nodes(conn, place))
        takes = home is not None and home.pool_id == pool.id
        if not takes:
            reasons = [f"takes the runs of @{pool.handle} alone"]
        elif not pool.nodes:
            reasons = [f"has no node: `evo-agents worker pool token create {pool.name}`, then join a machine with it"]
        elif not ready:
            reasons = [f"has {pool.nodes} node(s), and none is online, not draining and ready"]
        elif any(verdict.fit for verdict in verdicts):
            reasons = ["has a node that can take the run"]
        else:
            reasons = ["has a ready node, but none can take the run: see the workers"]
        fit = takes and any(verdict.fit for verdict in verdicts)
        candidate = PoolCandidate(
            pool=pool.name,
            agent=pool.handle,
            takes_run=takes,
            nodes=pool.nodes,
            ready_nodes=ready,
            fit=fit,
            reasons=reasons,
        )
        found.append(candidate)
    return found


def _candidate(verdict: Verdict, run, rank: int | None) -> WorkerCandidate:
    row, claimer = verdict.row, verdict.claimer
    runtimes = [*claimer.runtimes, *claimer.offered]
    reported = row.runtimes if isinstance(row.runtimes, dict) else {}
    models = sorted({model for name in runtimes for model in (reported.get(name) or {}).get("models") or ()})
    if verdict.fit:
        reasons = ["can take it now"] if not verdict.waiting else verdict.waiting
    else:
        reasons = verdict.blocking + verdict.waiting
    return WorkerCandidate(
        worker_id=row.id,
        worker=row.name,
        owner=row.owner,
        shared=row.owner_id != run.dispatched_by,
        fit=verdict.fit,
        rank=rank,
        reasons=reasons,
        is_run_worker=run.pinned_worker_id == row.id,
        online=bool(row.online),
        free_slots=max(row.slots - claimer.holding, 0),
        runtimes=runtimes,
        models=models,
        labels=list(claimer.node.labels),
        accounts=[],
    )


async def queued_candidates(conn: AsyncConnection, run_id: int, project: str, kind: str) -> RerunCandidates:
    """The answer of GET .../rerun-candidates for the queued run ``run_id``: each worker that could take it, the fit
    ones first (those that wait for nothing first, then by free slots), the agent pools of the project and the
    diagnosis."""
    found = await queue_fit(conn, run_id)
    run = found.run
    fit = sorted(
        (v for v in found.verdicts if v.fit),
        key=lambda v: (bool(v.waiting), v.claimer.holding - v.row.slots, v.row.id),
    )
    workers = [_candidate(v, run, place) for place, v in enumerate(fit, 1)]
    workers += [_candidate(v, run, None) for v in sorted((v for v in found.verdicts if not v.fit), key=_distance)]
    return RerunCandidates(
        run_id=run_id,
        project=project,
        kind=kind,
        state="queued",
        failure_cause=None,
        workers=workers,
        agents=[],
        accounts=[],
        pools=await _pools(conn, run.project_id, found.home, found.verdicts),
        diagnosis=found.diagnosis,
    )
