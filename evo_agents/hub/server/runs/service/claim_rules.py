"""What a worker's claim asks of a queued run: the projects and checkouts it serves, whose runs it may take, the
conditions each kind of run and the run's agent profile, browser, gateway and pools set (``Claimer``, ``ClaimShape``),
and the claim query built once per shape. The claim (``claims``) and the reasons a queued run waits (``queue_fit``)
both read a worker through ``read_claimer``."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from sqlalchemy import BigInteger, Text, and_, case, column, exists, func, literal, or_, select, true
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import ops_events, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import agent_placement, gateway_accounts, kg_shared_pool, pool_accounts, worker_placement
from evo_agents.hub.server.runs.models import available
from evo_agents.hub.server.runs.service import browsers, plugin_environments, plugin_runtimes
from evo_agents.hub.server.runs.service.profiles import claim_clause, run_packs, runtime_rule
from evo_agents.hub.server.runs.service.views import lost_here, steady
from evo_agents.hub.server.runs.service.worker_fit import unit_admits, writes

Condition = tuple[str, Any]  # a key that says which rule it is, and its SQL clause on tables.runs
WRITER_ROLES = [role for role in ("reader", "writer", "admin") if has_role(role, "writer")]


def _served(worker_id: int, owner_id: int):
    """The projects worker ``worker_id`` serves on which its owner holds writer: their ids and names."""
    wp, p, g = tables.worker_projects, tables.projects, tables.grants
    writer = exists().where(g.c.user_id == owner_id, g.c.project_id == wp.c.project_id, g.c.role.in_(WRITER_ROLES))
    return (
        select(wp.c.project_id, p.c.name)
        .join_from(wp, p, p.c.id == wp.c.project_id)
        .where(wp.c.worker_id == worker_id, writer)
    )


def _claimable(conditions: list[Condition]):
    """The oldest queued run that meets every one of ``conditions`` (``_claim_conditions``, ``Claimer.conditions``),
    locked, passing over one another claim holds locked. Each row is the run's id, the runtime it asked for, its kind,
    its profile's runtime axis, its project's id, its profile's environment axis and the agent packs its profile
    attaches."""
    r = tables.runs
    rules = (runtime_rule(), r.c.project_id, runtime_rule("environment"), run_packs())
    query = select(r.c.id, r.c.runtime, r.c.kind, *rules).where(*(clause for _, clause in conditions))
    return query.order_by(r.c.id).limit(1).with_for_update(of=r, skip_locked=True)


def _claim_conditions(
    worker_id: int,
    owner_id: int,
    projects: list[int],
    runtimes: list[str],
    pairs: list[tuple[int, str]],
    *,
    offered: tuple[str, ...] = (),
    plan_runs: bool,
    web_only: bool,
    review_runs: bool = False,
    judge_runs: bool = False,
    replay_runs: bool = False,
    history_runs: bool = False,
    probe_runs: bool = False,
    author_runs: bool = False,
    chat_runs: bool = False,
    task_runs: bool = False,
    question_runs: bool = False,
    release_runs: bool = False,
    browser_runs: bool = False,
    step_sessions: bool = False,
    clones: bool = False,
    steady: bool = True,
    shared: bool = False,
) -> list[Condition]:
    """The conditions on a queued run (``tables.runs``) for the worker to take it now, each with a key that says which
    rule it is (``queue_fit`` names why a run fails one): of its owner,
    or, on a ``shared`` worker, of any member who holds writer on its project now (``_dispatcher_clause``), in
    ``projects``, pinned to no other worker, asking for any runtime (when it has one of the core) or one of
    ``runtimes`` or ``offered`` (runtimes of plugins), of a repo it has a checkout of (``pairs`` of project id and
    repo), and, while the worker is not ``steady``, not the next attempt of a run it lost (``lost_here``). A run of one
    step needs a checkout of its repo, and a plan run one of every repo in its repos and a daemon of runs.PLAN_RUN_AGENT
    or later (``plan_runs``); a review run one of every repo in its repos and a daemon that says it runs review runs
    (``review_runs``), and a judge run, a replay run, a history run and a probe run likewise (``judge_runs``,
    ``replay_runs``, ``history_runs``, ``probe_runs``; a history run has no repo); an author run one of its first repo, the project's harness, and a
    daemon that says it runs author runs (``author_runs``): the worker
    leaves out the other repos it has no checkout of. A chat run is pinned to the worker the hub picked for it and
    needs a daemon that says it runs chat runs (``chat_runs``). A task run needs a checkout of one of its repos when it
    has any, a daemon that says it runs task runs (``task_runs``) and, when its agent type names workers, to be one of
    them; a question run likewise, with a daemon that says it runs question runs (``question_runs``). A release run needs a checkout of its one repo and a daemon that says it runs release runs
    (``release_runs``). A login session (kind browser) has no repo and needs a daemon that says it runs them
    (``browser_runs``). A plan run with a session per step needs a daemon that says it runs one (``step_sessions``).
    A worker whose daemon clones the repos it lacks (``clones``, runs.CLONES_CHECKOUTS) needs no checkout of any of
    them: those checkout rules hold only for an older one. A worker set to take runs dispatched from the web
    only (``web_only``) passes over the others, those dispatched before schema 0011 included. What the run's agent
    profile, its browser, the gateway, the agent pools and the worker's labels and taints ask of the worker is
    ``_gates``."""
    r = tables.runs
    pids = literal([pid for pid, _ in pairs], ARRAY(BigInteger))
    repos = literal([repo for _, repo in pairs], ARRAY(Text))
    held = func.unnest(pids, repos).table_valued("project_id", "repo").render_derived(name="c")
    checkouts = select(held.c.project_id, held.c.repo).cte("checkouts")
    needed = func.jsonb_array_elements(r.c.repos).table_valued(column("entry", JSONB)).render_derived(name="needed")
    # two levels down, the run is correlated by name: auto-correlation reaches only the enclosing level
    has_needed = (
        exists()
        .where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == needed.c.entry["repo"].astext)
        .correlate(r, needed)
    )
    every_repo = ~exists().select_from(needed).where(~has_needed)
    own_repo = exists().where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == r.c.repo)
    harness = exists().where(checkouts.c.project_id == r.c.project_id, checkouts.c.repo == r.c.repos[0]["repo"].astext)
    asked: list[Any] = [r.c.runtime.in_([*runtimes, *offered])]
    if runtimes:  # any takes a runtime of the core, never a plugin's
        asked.append(r.c.runtime == "any")
    conditions: list[Condition] = [
        ("queued", r.c.state == "queued"),
        ("project", r.c.project_id.in_(projects)),
        ("dispatcher", _dispatcher_clause(worker_id, owner_id, shared)),
        ("pinned", or_(r.c.pinned_worker_id.is_(None), r.c.pinned_worker_id == worker_id)),
        ("runtime", or_(*asked)),
    ]
    if not clones:  # an older daemon fails a run of a repo it has no checkout of
        checkout = case(
            (r.c.kind.in_(("plan", "review", "judge", "replay", "history", "probe", "release")), every_repo),
            (r.c.kind == "author", harness),
            (
                r.c.kind.in_(("chat", "browser")),
                true(),
            ),  # pinned to the worker the hub picked, which works without a repo it lacks
            (
                r.c.kind.in_(("task", "question")),
                or_(r.c.repos.is_(None), exists().select_from(needed).where(has_needed)),
            ),
            else_=own_repo,
        )
        conditions.append(("checkout", checkout))
    if web_only:
        conditions.append(("web_only", r.c.dispatched_via == "web"))
    if not plan_runs:
        conditions.append(("plan_runs", r.c.kind != "plan"))
    if not step_sessions:
        conditions.append(("step_sessions", r.c.session_per_step.is_(False)))
    said = {"review": review_runs, "judge": judge_runs, "replay": replay_runs, "history": history_runs}
    said |= {"probe": probe_runs}
    said |= {"author": author_runs, "chat": chat_runs, "release": release_runs, "browser": browser_runs}
    left_out = [kind for kind, runs_it in said.items() if not runs_it]  # the kinds its daemon does not say it runs
    if left_out:
        conditions.append(("kinds", r.c.kind.not_in(left_out)))
    typed = [kind for kind, runs_it in (("task", task_runs), ("question", question_runs)) if runs_it]
    typed_clause = or_(r.c.kind.not_in(("task", "question")), r.c.kind.in_(typed))
    conditions.append(("task_worker", typed_clause))
    if not steady:
        conditions.append(("lost_here", ~lost_here(worker_id)))
    return conditions


def dispatcher_parts(worker_id: int, owner_id: int, shared: bool) -> dict[str, Any]:
    """The parts of ``_dispatcher_clause``, by key: ``home`` (a run of the agent whose pool the worker is a node of, or a
    KG run of the shared KG pool it is a node of),
    ``own`` (its owner's run) and, on a shared worker, the parts of another member's run, ``writer``, ``unit`` and
    ``not_night`` (``member``), and ``alone`` (no run of another member held)."""
    r = tables.runs
    parts = {
        # a node of an agent's pool takes the agent's runs of any dispatcher, a node of the shared KG pool its KG runs
        "home": or_(agent_placement.home_run(worker_id), kg_shared_pool.takes(worker_id)),
        "own": r.c.dispatched_by == owner_id,
    }
    if not shared:
        return parts
    held = tables.runs.alias("held")
    other = exists().where(
        held.c.worker_id == worker_id,
        held.c.state.in_(runs.HELD_STATES),
        held.c.dispatched_by != r.c.dispatched_by,
    )
    return parts | {
        "writer": writes(r.c.dispatched_by, r.c.project_id),
        "unit": unit_admits(worker_id, r.c.dispatched_by),
        "not_night": r.c.kind.not_in(runs.CURATOR_KINDS),
        "alone": ~other,
    }


def _dispatcher_clause(worker_id: int, owner_id: int, shared: bool):
    """Whose queued runs worker ``worker_id`` of ``owner_id`` may take: its owner's alone; on a shared worker, also
    those of another member who holds writer on the run's project now and whom the units the worker is narrowed to
    admit (``worker_fit.unit_admits``), none of the night shift's kinds (``runs.CURATOR_KINDS``), and on any of them only while every run the worker holds is of the same member, so the
    agents of two members never run side by side as the daemon's user (``docs/workers.md``, Shared workers)."""
    parts = dispatcher_parts(worker_id, owner_id, shared)
    if not shared:
        return or_(parts["home"], parts["own"])
    member = and_(~parts["own"], parts["writer"], parts["unit"], parts["not_night"])
    return or_(parts["home"], and_(or_(parts["own"], member), parts["alone"]))


def resumes_here(worker_id: int):
    """Whether the run being claimed (``tables.runs``, correlated) resumes a run worker ``worker_id`` held, whose
    session and worktrees stay on its machine: pinned to it, with ``resume_of_run_id`` naming a run of it. The only
    runs a drained worker takes (``Claimer.conditions``): no other worker can, so a drain would hold them forever."""
    r = tables.runs
    parked = r.alias("parked")
    of_it = exists().where(parked.c.id == r.c.resume_of_run_id, parked.c.worker_id == worker_id)
    return and_(r.c.pinned_worker_id == worker_id, of_it)


async def resume_waits(conn: AsyncConnection, worker_id: int) -> bool:
    """Whether a queued run waits that resumes a run worker ``worker_id`` held (``resumes_here``): the heartbeat of a
    drained worker then tells its daemon to claim, and the claim hands it that run alone."""
    r = tables.runs
    query = select(exists().where(r.c.state == "queued", resumes_here(worker_id)))
    return (await conn.execute(query)).scalar_one()


def _checkout_pairs(served: dict[int, str], checkouts) -> list[tuple[int, str]]:
    """(project id, repo) of each of the worker's ``checkouts`` (keyed <project>/<repo>) in a project it ``served``."""
    by_name = {project: project_id for project_id, project in served.items()}
    pairs = []
    for checkout in checkouts or {}:
        project, _, repo = checkout.partition("/")
        if project in by_name and repo:
            pairs.append((by_name[project], repo))
    return pairs


@dataclass(frozen=True)
class Claimer:
    """What the claim of one worker goes by, read now (``read_claimer``): its row (``worker_columns``), its placement,
    how many runs it holds, its runtimes of the core and of plugins it takes (``offered``), the projects it serves on
    which its owner holds writer and its checkouts there (``pairs``), whether its daemon clones what it lacks, whether
    it is steady, and the plugins' environments it takes. The claim (``claims._claim_once``) and the reasons a queued
    run waits (``queue_fit``) both read a worker through it, so they never count it two ways."""

    row: Row[Any]
    node: worker_placement.Placement
    holding: int
    runtimes: list[str]
    offered: list[str]
    served: dict[int, str]
    pairs: list[tuple[int, str]]
    clones: bool
    steady: bool
    environments: list[str]

    def stops(self) -> list[str]:
        """What keeps the worker from taking any run now, before the queue is read, each by its key, in the order the
        claim checks them: unready (a node of a pool that is not ready), full (no free slot), runtime (none it takes),
        checkout (none of a project it serves, and a daemon that clones nothing), disk (its last heartbeat said less
        free disk on its worktrees' drive than its reserve, ``ops_events.disk_band``, so the run waits for another
        worker or for this one to free room instead of failing at its checkout). Draining is a condition on the run
        (``conditions``), since a drained worker still takes the runs that resume one it parked."""
        checks = (
            (not self.node.ready, "unready"),
            (self.holding >= self.row.slots, "full"),
            (not self.runtimes and not self.offered, "runtime"),
            (not self.pairs and not self.clones, "checkout"),
            (ops_events.disk_band(self.row.health) == "low", "disk"),
        )
        return [key for failed, key in checks if failed]

    def shape(self) -> ClaimShape:
        """What its ``conditions`` are built from."""
        row = self.row
        return ClaimShape(
            worker_id=row.id,
            owner_id=row.owner_id,
            name=row.name,
            drained=row.drained_at is not None,
            agent_version=row.agent_version,
            web_only=row.dispatch_from == "web",
            run_kinds=tuple(row.run_kinds or ()),
            features=tuple(row.features or ()),
            plugins=tuple(row.plugins or ()),
            shared=row.shared,
            served=tuple(self.served),
            runtimes=tuple(self.runtimes),
            offered=tuple(self.offered),
            pairs=tuple((project_id, repo) for project_id, repo in self.pairs),
            environments=tuple(self.environments),
            clones=self.clones,
            steady=self.steady,
            node=self.node,
        )

    def conditions(self) -> list[Condition]:
        """Every condition on a queued run for the worker to take it now, each with its key (``_conditions``)."""
        return list(_conditions(self.shape()))


@dataclass(frozen=True)
class ClaimShape:
    """What the conditions of a worker's claim are built from (``Claimer.shape``), hashable: the claim query of a worker
    whose shape has not changed since its last claim is reused (``_conditions``, ``claim_query``), as building it anew
    took every look of every waiting claim milliseconds of the api's one event loop. Each condition is SQL, so what
    the database holds is read when the query runs, never cached."""

    worker_id: int
    owner_id: int
    name: str
    drained: bool
    agent_version: str | None
    web_only: bool
    run_kinds: tuple[str, ...]
    features: tuple[str, ...]
    plugins: tuple[str, ...]
    shared: bool
    served: tuple[int, ...]
    runtimes: tuple[str, ...]
    offered: tuple[str, ...]
    pairs: tuple[tuple[int, str], ...]
    environments: tuple[str, ...]
    clones: bool
    steady: bool
    node: worker_placement.Placement


@lru_cache(maxsize=512)
def _conditions(at: ClaimShape) -> tuple[Condition, ...]:
    """Every condition on a queued run for the worker of ``at`` to take it now, each with its key: on a drained worker,
    that the run resumes one it parked (``resumes_here``), then ``_claim_conditions``, then ``_gates``."""
    drained = [("draining", resumes_here(at.worker_id))] if at.drained else []
    said = {kind: kind in at.run_kinds for kind in ("review", "judge", "replay", "history", "probe", "author", "chat")}
    said |= {kind: kind in at.run_kinds for kind in ("task", "question", "release", "browser")}
    conditions = _claim_conditions(
        at.worker_id,
        at.owner_id,
        list(at.served),
        list(at.runtimes),
        list(at.pairs),
        offered=at.offered,
        plan_runs=runs.takes_plan_runs(at.agent_version),
        web_only=at.web_only,
        **{f"{kind}_runs": runs_it for kind, runs_it in said.items()},
        step_sessions=runs.SESSION_PER_STEP in at.features,
        clones=at.clones,
        steady=at.steady,
        shared=at.shared,
    )
    return tuple(drained + conditions + _gates(at))


def _gates(at: ClaimShape) -> list[Condition]:
    """What the run's agent profile (``profiles.claim_clause``, with the plugins' environments the worker takes), its
    browser, the model gateway, a judge of merge requests, a review with no repo, the agent pools, the account pools
    and the worker's labels and taints ask of the worker of ``at``, each by its key."""
    clause = claim_clause(
        at.name,
        list(at.runtimes),
        at.features,
        list(at.offered),
        list(at.environments),
        list(at.plugins),
    )
    return [
        ("profile", clause),
        ("browser", browsers.claim_gate(at.worker_id, at.features)),
        ("gateway", gateway_accounts.claim_gate(at.agent_version)),
        ("merge_judge", merge_judge_gate(at.features)),
        ("repoless_review", repoless_review_gate(at.features)),
        ("agent_pool", agent_placement.claim_gate(at.worker_id)),
        ("pool_accounts", pool_accounts.claim_gate()),
        ("placement", or_(agent_placement.home_run(at.worker_id), at.node.gate(at.worker_id))),
    ]


@lru_cache(maxsize=512)
def claim_query(at: ClaimShape):
    """``_claimable`` of the conditions of ``at``, built once per shape; reusing the statement also keeps its cache
    key, so SQLAlchemy compiles it once."""
    return _claimable(list(_conditions(at)))


def repoless_review_gate(features):
    """The condition a queued run meets for a worker whose daemon says ``features``: a review run with no repo (the KG
    init of a project fed by uploads alone) goes only to a daemon that says it runs one (``runs.REPOLESS_REVIEW``)."""
    if runs.REPOLESS_REVIEW in (features or ()):
        return true()
    r = tables.runs
    return or_(r.c.kind != "review", func.coalesce(func.jsonb_array_length(r.c.repos), 0) > 0)


def merge_judge_gate(features):
    """The condition a queued run meets for a worker whose daemon says ``features``: a judge run of the merge queue or
    of a GitHub merge check (``merge_judge``, ``merge_check``) goes only to a daemon that says it judges merge requests
    (``runs.MERGE_JUDGE``); any other run, the Curator's judge runs too, asks nothing of it."""
    if runs.MERGE_JUDGE in (features or ()):
        return true()
    r, q, c = tables.runs, tables.merge_queue, tables.merge_checks
    judged = or_(exists().where(q.c.judge_run_id == r.c.id), exists().where(c.c.judge_run_id == r.c.id))
    return or_(r.c.kind != "judge", ~judged)


async def read_claimer(conn: AsyncConnection, row, node: worker_placement.Placement) -> Claimer:
    """The ``Claimer`` of the worker ``row`` (``worker_columns``) whose placement is ``node``, read on ``conn``."""
    r, w = tables.runs, tables.workers
    holding = select(func.count()).select_from(r).where(r.c.worker_id == row.id, r.c.state.in_(runs.HELD_STATES))
    reported, features = row.runtimes, row.features
    served = {found.project_id: found.name for found in await conn.execute(_served(row.id, row.owner_id))}
    steadiness = select(func.coalesce(steady(w), False)).where(w.c.id == row.id)
    return Claimer(
        row=row,
        node=node,
        holding=(await conn.execute(holding)).scalar_one(),
        runtimes=[runtime for runtime in runs.RUNTIMES if available((reported or {}).get(runtime))],
        offered=list(plugin_runtimes.claimable(reported, row.plugins, features)),
        served=served,
        pairs=_checkout_pairs(served, row.checkouts),
        clones=runs.CLONES_CHECKOUTS in (features or ()),
        steady=(await conn.execute(steadiness)).scalar_one(),
        environments=list(await plugin_environments.offered(conn, row.id, row.plugins, features)),
    )
