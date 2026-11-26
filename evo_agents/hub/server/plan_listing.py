"""The query of a project's plans list (GET /v1/projects/{project}/plans), and the step counts over a plan body that
the overview and the problems lists read too.

The columns are built in SQL over the jsonb body, so a list never reads a step's evidence or verify; ``plans.glance``
cuts the goal and the steps further in Python.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Label, Select, bindparam, case, cast, column, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH, aggregate_order_by

from evo_agents.hub import tables


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
