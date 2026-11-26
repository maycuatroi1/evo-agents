"""The plans of a project in the database: tables plans and plan_revisions (``PlanStore``), the query of a project's
plans list (GET /v1/projects/{project}/plans), and the step counts over a plan body that the overview and the problems
lists read too.

The columns of the list are built in SQL over the jsonb body, so a list never reads a step's evidence or verify;
``plans.glance`` cuts the goal and the steps further in Python.

A plan keeps its revisions, but not as a ``RevisionedStore`` does: its row is locked (``FOR UPDATE``) rather than its
project's records, it is named by its id rather than a number, its row holds the body itself, and a revision holds the
whole body with a summary of the change and the run that wrote it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from sqlalchemy import (
    ColumnElement,
    Label,
    Select,
    bindparam,
    case,
    cast,
    column,
    func,
    insert,
    literal,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH, aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.plans.models import Held


def step_counts(body: ColumnElement) -> tuple[Label, Label]:
    """Two columns over ``body``, the JSONB expression of a plan body: how many steps it has (``steps_total``), and
    how many of them are done (``steps_done``), as GET .../plans and GET /v1/me/overview count them. Steps that are
    not an array count as none."""
    steps = body["steps"]
    listed = func.jsonb_typeof(steps) == "array"
    step = func.jsonb_array_elements(steps, type_=JSONB).column_valued("s")
    done = (
        select(func.count())
        .where(func.jsonb_typeof(step) == "object", step["status"].astext.in_(["done", "descoped"]))
        .scalar_subquery()
    )
    return (
        case((listed, func.jsonb_array_length(steps)), else_=0).label("steps_total"),
        case((listed, done), else_=0).label("steps_done"),
    )


STEP_WHAT_CHARS = 200  # of a step's what, the list's fallback for its title (plans.glance)
GOAL_READ_CHARS = 1000  # of a plan's goal, which plans.glance cuts further
REPO_NAMES = "$.repos[*].repo"  # the names of a plan's repos, those that are objects with one


def step_glances(body: ColumnElement) -> Label:
    """A column over ``body``, the JSONB expression of a plan body: each step's id, order, status, title and the start
    of its what, in order, for ``plans.glance``, so a list never reads a step's evidence or verify. None when the steps
    are not an array; a step that is not an object gives nulls."""
    steps = body["steps"]
    each = func.jsonb_array_elements(steps).table_valued(column("value", JSONB), with_ordinality="n").render_derived()
    step = each.c.value
    item = func.jsonb_build_object(
        "id",
        step["id"],
        "order",
        step["order"],
        "status",
        step["status"],
        "title",
        step["title"],
        "what",
        func.left(step["what"].astext, STEP_WHAT_CHARS),
    )
    projected = (
        select(func.jsonb_agg(aggregate_order_by(item, each.c.n), type_=JSONB)).select_from(each).scalar_subquery()
    )
    return case((func.jsonb_typeof(steps) == "array", projected), else_=None).label("steps")


def listed(project_id, area) -> Select:
    """The plans of project ``project_id`` with what PlanSummary shows, column by field name (goal, steps and repos
    the way ``plans.glance`` reads them), and their label; those of ``area`` alone when given. Built once per
    shape (LISTED) with bind parameters for both."""
    plans, users = tables.plans, tables.users
    query = (
        select(
            plans.c.plan_id,
            plans.c.area,
            plans.c.label,
            plans.c.revision,
            plans.c.digest,
            plans.c.body["title"].astext.label("title"),
            plans.c.updated_at,
            users.c.login.label("updated_by"),
            *step_counts(plans.c.body),
            plans.c.body["problems"].label("problems"),
            func.left(plans.c.body["goal"].astext, GOAL_READ_CHARS).label("goal"),
            step_glances(plans.c.body),
            func.jsonb_path_query_array(plans.c.body, cast(literal(REPO_NAMES), JSONPATH), type_=JSONB).label("repos"),
        )
        .join_from(plans, users, users.c.id == plans.c.updated_by)
        .where(plans.c.project_id == project_id)
    )
    if area is not None:
        query = query.where(plans.c.area == area)
    return query.order_by(plans.c.plan_id)


# GET .../plans, with an area and without one: :project_id, :area
LISTED = {False: listed(bindparam("project_id"), bindparam("area")), True: listed(bindparam("project_id"), None)}


class PlanStore:
    """Reads and writes of the rows of a project's plans and their revisions."""

    async def held(self, conn: AsyncConnection, project_id: int, plan_id: str, *, lock: bool = False) -> Held | None:
        """The plan as held, its row locked until the transaction ends with ``lock``. The lock is taken on the plan
        alone: a locking query joined to users would, after waiting for another writer, recheck the join against the
        old updater and lose the row. The read after it sees the other writer's commit (a new snapshot per
        statement)."""
        plans, users = tables.plans, tables.users
        this = (plans.c.project_id == project_id, plans.c.plan_id == plan_id)
        if lock:
            locked = select(plans.c.plan_id).where(*this).with_for_update()
            if (await conn.execute(locked)).first() is None:
                return None
        held = select(
            plans.c.plan_id,
            plans.c.area,
            plans.c.label,
            plans.c.body,
            plans.c.revision,
            plans.c.digest,
            plans.c.created_at,
            plans.c.updated_at,
            users.c.login.label("updated_by"),
        ).join_from(plans, users, users.c.id == plans.c.updated_by)
        row = (await conn.execute(held.where(*this))).first()
        return Held(**row._mapping) if row else None

    async def listed(self, conn: AsyncConnection, project_id: int, area: str | None) -> list:
        """The rows of ``LISTED``: the project's plans, those of ``area`` alone when given."""
        return list((await conn.execute(LISTED[area is None], {"project_id": project_id, "area": area})).all())

    async def revisions(self, conn: AsyncConnection, project_id: int, plan_id: str, revision: int | None = None):
        """The revisions of the plan with what RevisionBody shows by column name, and their label, the latest first;
        revision ``revision`` alone when given."""
        revisions, users = tables.plan_revisions, tables.users
        query = (
            select(
                revisions.c.revision,
                revisions.c.area,
                revisions.c.label,
                revisions.c.digest,
                revisions.c.summary,
                users.c.login.label("actor"),
                revisions.c.created_at,
                revisions.c.run_id,
                revisions.c.body,
            )
            .join_from(revisions, users, users.c.id == revisions.c.actor_id)
            .where(revisions.c.project_id == project_id, revisions.c.plan_id == plan_id)
        )
        if revision is not None:
            query = query.where(revisions.c.revision == revision)
        return list((await conn.execute(query.order_by(revisions.c.revision.desc()))).all())

    async def write(
        self, conn: AsyncConnection, project_id: int, plan_id: str, held: Held | None, values: dict, user_id: int
    ):
        """Write the plan's row with ``values``: a new one when ``held`` is None (nothing when another request created
        it first), else the next revision of ``held``. (the body as stored, created_at, updated_at); None when nothing
        was written."""
        plans = tables.plans
        if held is None:
            row = {"project_id": project_id, "plan_id": plan_id, **values, "updated_by": user_id}
            written = pg_insert(plans).values(**row).on_conflict_do_nothing()
        else:
            written = (
                update(plans)
                .values(**values, updated_at=func.now(), updated_by=user_id)
                .where(plans.c.project_id == project_id, plans.c.plan_id == plan_id)
            )
        return (await conn.execute(written.returning(plans.c.body, plans.c.created_at, plans.c.updated_at))).first()

    async def add_revision(self, conn: AsyncConnection, project_id: int, values: dict) -> None:
        """Append a row of plan_revisions to the plan's."""
        await conn.execute(insert(tables.plan_revisions).values(project_id=project_id, **values))

    async def problem_numbers(self, conn: AsyncConnection, project_id: int, numbers: list[int]) -> set[int]:
        """Those of ``numbers`` that are problems of the project."""
        p = tables.problems
        query = select(p.c.number).where(p.c.project_id == project_id, p.c.number.in_(numbers))
        return set((await conn.execute(query)).scalars())

    async def known_repos(self, conn: AsyncConnection, project_id: int) -> set[str]:
        """The repos registered for the project, and its harness repo by its directory name."""
        repos, projects = tables.project_repos, tables.projects
        names = set((await conn.execute(select(repos.c.name).where(repos.c.project_id == project_id))).scalars())
        harness = select(projects.c.harness_path).where(projects.c.id == project_id)
        harness_path = (await conn.execute(harness)).scalar()
        if harness_path:
            names.add(PurePosixPath(harness_path).name)  # the harness repo goes by its directory name, as in the loader
        return names


STORE = PlanStore()
