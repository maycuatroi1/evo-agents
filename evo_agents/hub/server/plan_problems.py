"""The problems a plan names (``problems`` of plan.schema.json, plan problem-oriented-hub, criterion a4), as the plan
routes (``evo_agents.hub.server.plans``) check them on every write.

A write that names a problem anew, one the plan held before does not, names a problem of its project
(``evo_agents.hub.problems``): a code the project lacks refuses the write (``missing``, ``refusal``). A code once
held is not looked up again, as a problem is closed and never deleted. A PUT that writes a plan or a revision naming
no problem is stored with the warning NO_PROBLEMS (``warnings``): naming one is asked, never required.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.problems import number_of
from evo_agents.hub.server.plan_models import Problem

PATH = "problems"  # where in a plan its problems are, as a warning or a refusal names it
NO_PROBLEMS = (
    "the plan names no problem of the project it solves: add problems: [P-<number>], as `evo-agents hub problem list` "
    "lists them"
)


def named(body) -> list[str]:
    """The codes of problems a plan body names, as written (plan.schema.json keeps them P-<number>, each once)."""
    found = body.get("problems") if isinstance(body, dict) else None
    return [code for code in found if isinstance(code, str)] if isinstance(found, list) else []


async def missing(conn: AsyncConnection, project_id: int, old: dict | None, new: dict) -> list[str]:
    """The codes ``new`` names that ``old`` did not and the project has no problem of, in the order ``new`` names
    them."""
    held = set(named(old))
    added = [code for code in named(new) if code not in held]
    numbers = {code: number_of(code) for code in added}
    wanted = [number for number in numbers.values() if number is not None]
    found: set[int] = set()
    if wanted:
        p = tables.problems
        query = select(p.c.number).where(p.c.project_id == project_id, p.c.number.in_(wanted))
        found = set((await conn.execute(query)).scalars())
    return [code for code in added if numbers[code] not in found]


def refusal(project: str, plan_id: str, codes: list[str]) -> tuple[str, list[dict]]:
    """The message and detail of the 422 of a write naming ``codes``, which project ``project`` does not have."""
    which = f"problem {codes[0]}" if len(codes) == 1 else f"problems {', '.join(codes)}"
    message = (
        f"plan {plan_id} names {which}, which project {project} does not have: see `evo-agents hub problem list`. "
        "Nothing was written"
    )
    return message, [{"path": PATH, "message": f"no problem {code} in project {project}"} for code in codes]


def warnings(body: dict) -> list[Problem]:
    """The warning of a plan written with no problem named; none for a plan that names one."""
    return [] if named(body) else [Problem(path=PATH, message=NO_PROBLEMS)]
