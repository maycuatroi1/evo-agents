"""The hand-out of the Loop (plan loop-engine, criterion a6): ``hand_out``, the ``admit.Dispatch`` every tick uses,
which builds the run of a work item that passed admission through a run kind the hub has already, in the tick's
transaction. The admission pass writes the rest: the work item running with its run, its attempts, its ledger row.

- ``brainstorm``: a task run of agent type strategist of the plugin canvas, through ``hand_own_task``
  (``caps.hand_task``): the goal, its metric and target and the canvas it drives (``texts.brainstorm_task``).
- ``use_case``: a task run of agent type ba of the plugin srs, the same way: the claim, its test card, the goal and
  the use cases it links, and once a reviewer asked for changes of the version it drafted, their words
  (``texts.use_case_task``). ``srs__propose`` takes that run's version, written as the goal's owner (``runs_of``).
- ``build``: an author run of the frame task, in Loop mode (``author.build_author_prompt``), its request the frame
  task, its child tasks as the plan's steps, the use case at its version, the claim and its test card
  (``texts.author_request``).
- ``deliver``: a plan run of the plan the build's author run wrote, over the repos of its steps not done.

An author run and a plan run go to worker ``loop.worker``, pinned, in the name of the worker's owner, who must hold
writer on the project (the checkpoint of step 1, answer 6), dispatched with a machine credential, with the caps of
``loop.caps`` (``caps.run_budget``) and the project's default agent profile; each is linked to the frame task (a link
``run`` of the plugin tasks) and audited as ``loop.dispatch``. A worker that cannot take it (its daemon, its checkouts,
the owner's role), an author run whose harness no project of the owner's lists an origin for (``repo_origins``, as the
run's leases read it: the worker's preflight would fail the run, cause origin), or a plan that cannot run (no step
left, a step with no repo, a repo without its origin, another run of the plan active), hands out nothing: the work
item stays queued with reason ``worker`` and the hub's why. The Loop merges nothing and starts no job: a plan run's
merge requests go through the merge queue as any plan run's do.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.loop import caps, texts
from evo_agents.hub.server.loop.admit import WORKER, Handed, LoopWorker
from evo_agents.hub.server.loop.model import BRAINSTORM, BUILD, DELIVER, USE_CASE, ItemView, View
from evo_agents.hub.server.repo_origins import repo_origins

log = logging.getLogger(__name__)

DISPATCH = "loop.dispatch"  # a run the Loop handed out: "<project> item:<id> run:<id> kind=<kind>"
DISPATCHED_VIA = "machine"  # the credential of the Loop's runs: no web session, so a web-only worker takes none
PLAN_RUNTIME = "any"


async def _project(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return await conn.scalar(select(p.c.name).where(p.c.id == project_id))


def _refused(why: str) -> Handed:
    return Handed(reason=WORKER, why=why)


async def _owner(conn: AsyncConnection, project_id: int, worker: LoopWorker) -> tuple[str | None, str | None]:
    """(the login of the owner of worker ``worker``, why the runs cannot go in that name: no writer role)."""
    u, g = tables.users, tables.grants
    login = await conn.scalar(select(u.c.login).where(u.c.id == worker.owner_id))
    role = await conn.scalar(select(g.c.role).where(g.c.user_id == worker.owner_id, g.c.project_id == project_id))
    if not has_role(role, "writer"):
        return login, (
            f"{login}, the owner of worker {worker.name} (the policy's loop.worker), holds {role or 'no grant'} on the "
            "project, not writer: the Loop's author and plan runs go in that name"
        )
    return login, None


async def _unfit(conn: AsyncConnection, project: str, worker: LoopWorker, kind: str, repos, runtime) -> str | None:
    """Why ``worker`` could never claim a run of ``kind`` over ``repos`` on ``runtime``; None when it can."""
    from evo_agents.hub.server.runs.service.dispatch import unfit, worker_as_pinned  # it imports the routes' modules

    pinned = await worker_as_pinned(conn, worker.id)
    problems = ["it is gone"] if pinned is None else unfit(pinned, project, kind, list(repos), runtime)
    return f"worker {worker.name} cannot take the {kind} run: {'; '.join(problems)}" if problems else None


async def _insert(conn: AsyncConnection, values: dict) -> tuple[int | None, str | None]:
    """(the run made of ``values``, why the database refused it), in a savepoint; queued for its worker."""
    from evo_agents.hub.server.run_state import notify_queued
    from evo_agents.hub.server.runs.service.dispatch import insert_run

    try:
        run_id = await insert_run(conn, values)
    except IntegrityError as exc:
        return None, f"the hub refused the run: {type(exc.orig).__name__}"
    await notify_queued(conn, run_id)
    return run_id, None


async def _handed(conn: AsyncConnection, project: str, project_id: int, item: ItemView, run_id: int, owner) -> None:
    """The run's first event, its link on the frame task, and the audit row."""
    from evo_agents.hub.server.run_state import write_event

    text = f"Queued by the Loop of project {project} for work item #{item.id} ({item.key}), as {owner[0]}."
    await write_event(conn, run_id, {"text": text, "loop_item": item.id})
    if item.task_id is not None:
        link = {"task_id": item.task_id, "kind": "run", "run_id": run_id, "created_by": owner[1]}
        await conn.execute(insert(tables.tasks.links).values(**link))
    await audit.record(
        conn,
        actor_id=owner[1],
        token_id=None,
        action=DISPATCH,
        target=f"{project} item:{item.id} run:{run_id} kind={item.kind}",
        project_id=project_id,
    )


def _values(project_id: int, kind: str, item: ItemView, view: View, worker: LoopWorker, runtime: str) -> dict:
    caps_of = BUILD if kind == "author" else DELIVER
    return {
        "kind": kind,
        "project_id": project_id,
        "dispatched_by": worker.owner_id,
        "dispatched_via": DISPATCHED_VIA,
        "pinned_worker_id": worker.id,
        "requested_runtime": runtime,
        "runtime": runtime,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": caps.timeout_s(caps_of, view.policy),
        "budget": caps.run_budget(caps_of, view.policy),
    }


async def _harness_origin(conn: AsyncConnection, project_id: int, project: str, worker: LoopWorker, harness: str):
    """Why an author run in the name of ``worker``'s owner cannot reach the harness ``harness``: no project of the
    owner's lists an origin for it, so no credential is leased for it and the worker's preflight fails the run; None
    when one does."""
    if harness in await repo_origins(conn, project_id, worker.owner_id, [harness]):
        return None
    return (
        f"the author run needs the harness {harness}, for which neither project {project} nor another project of the "
        f"owner of worker {worker.name} lists an origin"
    )


async def _author_run(conn: AsyncConnection, project_id: int, item: ItemView, view: View, worker: LoopWorker):
    """The author run of build ``item`` on ``worker``, in Loop mode."""
    from evo_agents.hub.server.runs.service.dispatch import author_repos, worker_as_pinned

    project = await _project(conn, project_id)
    login, problem = await _owner(conn, project_id, worker)
    pinned = await worker_as_pinned(conn, worker.id)
    repos = None if pinned is None else await author_repos(conn, project_id, project, pinned.checkouts)
    if problem is not None or repos is None:
        return _refused(problem or "the project was registered without its harness, where plans live")
    harness, runtime = repos[0]["repo"], author.AUTHOR_RUNTIMES[0]
    problem = await _harness_origin(conn, project_id, project, worker, harness)
    problem = problem or await _unfit(conn, project, worker, "author", [harness], runtime)
    if problem is not None:
        return _refused(problem)
    request = await texts.author_request(conn, project_id, project, item)
    title = f"{author.TITLE_PREFIX}the Loop's build of {item.use_case or item.key}"
    values = _values(project_id, "author", item, view, worker, runtime) | {
        "title": runs.step_title({"title": title}),
        "repos": repos,
        "request": request,
    }
    run_id, problem = await _insert(conn, values)
    if run_id is None:
        return _refused(problem)
    await _handed(conn, project, project_id, item, run_id, (login, worker.owner_id))
    return Handed(run_id=run_id, owner=login)


async def _plan_repos(conn: AsyncConnection, project_id: int, plan_id: str | None):
    """(the plan's revision and title, the repos of its plan run), or (None, why it cannot run)."""
    from evo_agents.hub.server.runs.service.dispatch import plan_run_repos

    p = tables.plans
    query = select(p.c.revision, p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan_id)
    held = (await conn.execute(query)).first() if plan_id else None
    if held is None:
        return None, f"plan {plan_id} is not on the hub"
    try:
        repos = plan_run_repos(SimpleNamespace(plan_id=plan_id, body=held.body))
    except HTTPException as exc:
        return None, str(exc.detail)
    pr = tables.project_repos
    names = [entry["repo"] for entry in repos]
    query = select(pr.c.name).where(pr.c.project_id == project_id, pr.c.name.in_(names), pr.c.origin.is_not(None))
    known = set(await conn.scalars(query))
    missing = [name for name in names if name not in known]
    if missing:
        return None, f"plan {plan_id} needs {', '.join(missing)}, for which the project lists no origin"
    return (held.revision, runs.step_title(held.body), repos), None


async def _plan_run(conn: AsyncConnection, project_id: int, item: ItemView, view: View, worker: LoopWorker):
    """The plan run of deliver ``item`` on ``worker``."""
    project = await _project(conn, project_id)
    login, problem = await _owner(conn, project_id, worker)
    if problem is not None:
        return _refused(problem)
    held, problem = await _plan_repos(conn, project_id, item.plan_id)
    if held is None:
        return _refused(problem)
    revision, title, repos = held
    problem = await _unfit(conn, project, worker, "plan", [entry["repo"] for entry in repos], PLAN_RUNTIME)
    if problem is not None:
        return _refused(problem)
    values = _values(project_id, "plan", item, view, worker, PLAN_RUNTIME) | {
        "plan_id": item.plan_id,
        "plan_revision": revision,
        "title": title,
        "repos": repos,
    }
    run_id, problem = await _insert(conn, values)
    if run_id is None:
        return _refused(f"plan {item.plan_id} has an active run already, or {problem}")
    await _handed(conn, project, project_id, item, run_id, (login, worker.owner_id))
    return Handed(run_id=run_id, owner=login)


async def _task_run(conn: AsyncConnection, project_id: int, item: ItemView) -> Handed:
    """The task run of brainstorm or use_case ``item`` for its agent type."""
    project = await _project(conn, project_id)
    if item.kind == BRAINSTORM:
        task, title = await texts.brainstorm_task(conn, project, item)
    else:
        task, title = await texts.use_case_task(conn, project, item)
    handed = await caps.hand_task(conn, project_id, item.kind, task, title)
    if handed.run_id is not None:
        from evo_agents.hub.server.run_state import write_event

        text = f"Queued by the Loop of project {project} for work item #{item.id} ({item.key})."
        await write_event(conn, handed.run_id, {"text": text, "loop_item": item.id})
    return handed


async def hand_out(
    conn: AsyncConnection, project_id: int, item: ItemView, view: View, worker: LoopWorker
) -> Handed | None:
    """The run of work item ``item``, which passed admission, as the module says; None for no kind of the Loop's."""
    if item.kind in (BRAINSTORM, USE_CASE):
        return await _task_run(conn, project_id, item)
    if item.kind == BUILD:
        return await _author_run(conn, project_id, item, view, worker)
    if item.kind == DELIVER:
        return await _plan_run(conn, project_id, item, view, worker)
    return None


__all__ = ["DISPATCH", "hand_out"]
