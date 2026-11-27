"""The rows of the plugin API's task runs (``evo_agents.hub.server.task_dispatch``): the plugin enabled in a project,
the grant of a type's run_as, the branches task runs work on, a task run with its names, its next attempt, its prompt's
parts, and the repo a push names."""

from __future__ import annotations

from sqlalchemy import null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import plugin_calls, runs, tables
from evo_agents.hub.server.run_retry import next_attempt


def task_select():
    """A task run with its project's and agent type's names."""
    r, t, p = tables.runs, tables.project_agent_types, tables.projects
    return (
        select(
            r.c.id,
            r.c.attempt,
            p.c.name.label("project"),
            r.c.project_id,
            t.c.name.label("agent_type"),
            t.c.plugin_id,
            r.c.state,
            r.c.branch,
            r.c.pulls,
            r.c.failure_cause,
            r.c.queued_at,
            r.c.finished_at,
        )
        .join_from(r, t, t.c.id == r.c.agent_type_id)
        .join(p, p.c.id == r.c.project_id)
    )


async def enabled_in_named(conn: AsyncConnection, plugin_id: int, project: str):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in the project named ``project``, or None."""
    query = plugin_calls.enabled_query().where(tables.plugins.c.id == plugin_id, tables.projects.c.name == project)
    return (await conn.execute(query)).first()


async def enabled_in_project(conn: AsyncConnection, plugin_id: int, project_id: int):
    """The row of ``plugin_calls.enabled_query`` of plugin ``plugin_id`` in project ``project_id``, or None."""
    query = plugin_calls.enabled_query().where(tables.plugins.c.id == plugin_id, tables.projects.c.id == project_id)
    return (await conn.execute(query)).first()


async def role_of(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    g = tables.grants
    query = select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def default_branch(conn: AsyncConnection, project_id: int, repo: str) -> str | None:
    pr = tables.project_repos
    query = select(pr.c.default_branch).where(pr.c.project_id == project_id, pr.c.name == repo)
    return (await conn.execute(query)).scalar_one_or_none()


async def busy_on(conn: AsyncConnection, project_id: int, repo: str, branch: str) -> int | None:
    """A task run of the project that is not over and works on ``branch`` of ``repo``, or None."""
    r = tables.runs
    busy = select(r.c.id).where(
        r.c.project_id == project_id,
        r.c.kind == "task",
        r.c.state.in_(runs.ACTIVE_STATES),
        r.c.repos.contains([{"repo": repo, "branch": branch}]),
    )
    return (await conn.execute(busy.limit(1))).scalar_one_or_none()


async def set_branches(conn: AsyncConnection, run_id: int, branch: str, repos: list[dict]) -> None:
    """Give run ``run_id`` its branch and its repos (SQL NULL for none), with no pull request yet."""
    r = tables.runs
    await conn.execute(update(r).values(branch=branch, repos=repos or null(), pulls=[]).where(r.c.id == run_id))


async def task_type_of(conn: AsyncConnection, run_id: int):
    """(project_id, plugin_id, name) of the agent type of task run ``run_id``, or None for no task run of one."""
    r, t = tables.runs, tables.project_agent_types
    query = select(r.c.project_id, t.c.plugin_id, t.c.name).join_from(r, t, t.c.id == r.c.agent_type_id)
    return (await conn.execute(query.where(r.c.id == run_id, r.c.kind == "task"))).one_or_none()


async def queue_attempt(conn: AsyncConnection, run_id: int, max_usd: float, worker_id: int | None) -> int:
    """The next attempt of run ``run_id`` (``run_retry.next_attempt``), its budget's max_usd ``max_usd``, and pinned to
    ``worker_id`` instead when the run was pinned and ``worker_id`` is given; its id."""
    r = tables.runs
    new_id = (await conn.execute(next_attempt(run_id))).scalar_one()
    budget = (await conn.execute(select(r.c.budget).where(r.c.id == new_id))).scalar_one()
    budget = (budget if isinstance(budget, dict) else {}) | {"max_usd": max_usd}
    await conn.execute(update(r).values(budget=budget).where(r.c.id == new_id))
    if worker_id is not None:
        pinned = update(r).values(pinned_worker_id=worker_id)
        await conn.execute(pinned.where(r.c.id == new_id, r.c.pinned_worker_id.is_not(None)))
    return new_id


async def task_by_run(conn: AsyncConnection, run_id: int):
    """Task run ``run_id`` with its names (``task_select``)."""
    return (await conn.execute(task_select().where(tables.runs.c.id == run_id))).one()


async def latest_task(conn: AsyncConnection, project_id: int, plugin_id: int, branch: str):
    """The latest attempt of the task run of plugin ``plugin_id`` on ``branch`` in the project, or None."""
    r = tables.runs
    query = task_select().where(
        r.c.project_id == project_id,
        tables.project_agent_types.c.plugin_id == plugin_id,
        r.c.branch == branch,
    )
    return (await conn.execute(query.order_by(r.c.attempt.desc()).limit(1))).one_or_none()


async def prompt_parts(conn: AsyncConnection, run_id: int):
    """(profile_id, name, plugin) of run ``run_id``: its agent type's and plugin's names, None for a member's task."""
    r, t, pl = tables.runs, tables.project_agent_types, tables.plugins
    query = (
        select(r.c.profile_id, t.c.name, pl.c.name.label("plugin"))
        .select_from(r.outerjoin(t, t.c.id == r.c.agent_type_id).outerjoin(pl, pl.c.id == t.c.plugin_id))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one()


async def member_task_given(conn: AsyncConnection, task_id: int):
    """(input, assets) of member task ``task_id``, or None."""
    m = tables.member_tasks
    return (await conn.execute(select(m.c.input, m.c.assets).where(m.c.task_id == task_id))).one_or_none()


async def ended_row(conn: AsyncConnection, run_id: int):
    """(agent_type_id, project_id, name, attempt, branch, failure_cause) of run ``run_id``, its project's name."""
    r, p = tables.runs, tables.projects
    query = select(r.c.agent_type_id, r.c.project_id, p.c.name, r.c.attempt, r.c.branch, r.c.failure_cause)
    return (await conn.execute(query.join_from(r, p, p.c.id == r.c.project_id).where(r.c.id == run_id))).one()


async def pushed_row(conn: AsyncConnection, run_id: int):
    """(worker_id, state, kind, project_id, branch, repos, title) of run ``run_id``, or None."""
    r = tables.runs
    query = select(r.c.worker_id, r.c.state, r.c.kind, r.c.project_id, r.c.branch, r.c.repos, r.c.title)
    return (await conn.execute(query.where(r.c.id == run_id))).one_or_none()


async def repo_origin(conn: AsyncConnection, project_id: int, repo: str):
    """(origin, default_branch) of repo ``repo`` of the project, or None."""
    pr = tables.project_repos
    query = select(pr.c.origin, pr.c.default_branch).where(pr.c.project_id == project_id, pr.c.name == repo)
    return (await conn.execute(query)).one_or_none()


async def type_and_plugin(conn: AsyncConnection, run_id: int) -> tuple[str, str]:
    """(the agent type's name, its plugin's name) of task run ``run_id``."""
    t, pl, r = tables.project_agent_types, tables.plugins, tables.runs
    query = select(t.c.name, pl.c.name).join_from(r, t, t.c.id == r.c.agent_type_id)
    agent_type, plugin = (await conn.execute(query.join(pl, pl.c.id == t.c.plugin_id).where(r.c.id == run_id))).one()
    return agent_type, plugin
