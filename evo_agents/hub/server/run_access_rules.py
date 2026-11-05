"""What the token of a run that runs as its owner never reaches, what its owner may let it reach, what starts no run
while it is answered, and the audit row of each write made with it (``evo_agents.hub.server.run_access``).

A run's token (``security.RUN``, ``evr_...``) is its owner with the owner's grants on every project, and hub admin when
the owner is one; ``Authenticate`` refuses it, with 403 and the reason, every path of the groups of REFUSED, whatever
the method, and every write (POST, PUT, PATCH, DELETE) of the groups of WRITES_REFUSED. A group is a closed list of
path patterns, each matching its prefix and everything under it (a pattern that ends in ``$`` matches that path
alone), so a route added later under a group's prefix is refused with the group until someone takes it out of the
group: secrets and everything that holds a credential's value (an agent's accounts and provider accounts among them),
tokens and signing in (whoami aside), the worker protocol with the terminal, the live view, takeover and handback of a
run, and the run access of any run, its own included, with the owner's run reach below, so no run grants running as
its owner onward or widens what it reaches; workers and their pools; and no write of grants and users, of the owner's
autonomy (preferences, stops, policies, the org chart but its workers), of agent profiles and types, no run started or
messaged, no decision answered and no approval given, and nothing of the Curator's. What the agent of a run never does
on a route open to it is the route's to refuse, as for the agent of a run on /mcp: each of those rules tests
``Principal.acts_for_run``, which both have.

Five of those groups are the owner's to open, REACH_GROUPS: ``agents`` (the writes of a project's agent profiles and
agent types), ``workers`` (workers, worker pools, their join tokens and labels, and the org chart's workers),
``curator`` (the Curator's writes), ``releases`` (setting a project's release schedule, resuming it and checking it
now, which may queue the cut of a release as the schedule's owner) and ``answers`` (asking an answer job of a project's
graph, ``kg_answers.ask``, whose batches the hub queues later as review runs dispatched as the job's sender, the
owner). The member's run reach, ``users.run_reach``, set with PUT /v1/autonomy/run-reach (``evo-agents hub autonomy
run-reach --allow ...``), is the set of them the tokens of their runs skip; ``security`` reads it with the token, so a
change holds from the next request of every run. It is empty unless the member chose, which refuses as before, and a
refusal of one of them says how the owner opens it. Every other group stays refused whatever the owner chose.

While the middleware answers a request of a run's token, ACTING_RUN holds the run's id, and no run is queued whatever
route the request took: ``refuse_new_run`` is the last word of every place that inserts a run
(``runs.service.dispatch.insert_run``, the session of a chat run and a parked run's resumption), and
``new_run_refusal`` the reason a place that refuses with words (a mention in a chat, a core plugin's task) says. The
one exception is the release check now, which the middleware let through only when the owner opened ``releases``: it
queues its cut inside ``release_dispatch``, as the schedule's owner, as the daily check would.

``record_write`` adds the audit row run_access.write of each write made with a run's token, refused or not, once its
answer started: the run, the method, the path and the status, never a query string, a body or a value; the actor is the
owner and the token the run's.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.credentials import RUN_REACH_GROUPS
from evo_agents.hub.server import audit

log = logging.getLogger(__name__)

_UNDER = r"(?:/.*)?"  # the prefix itself and every path under it
_PROJECT = r"/v1/projects/[^/]+"
_RUN = rf"{_PROJECT}/runs/[^/]+"
# The groups a member may let the tokens of their runs reach (``users.run_reach``), in the order the hub keeps them.
REACH_GROUPS = RUN_REACH_GROUPS
AGENTS, WORKERS, CURATOR, RELEASES, ANSWERS = REACH_GROUPS
RUN_REACH_PATH = "/v1/autonomy/run-reach"
# (group, why, path patterns): every method of a path in one of them is refused
REFUSED = (
    (
        "secrets",
        "secrets and everything that holds a credential's value",
        (
            "/v1/secrets",
            "/v1/shares",
            "/v1/accounts",
            "/v1/account-policies",
            "/v1/agent-accounts",
            "/v1/provider-accounts",
            rf"{_PROJECT}/agents/[^/]+/(?:accounts|provider-accounts)",
            rf"{_PROJECT}/browser-identities",
            rf"{_RUN}/(?:credentials|accounts)",
        ),
    ),
    (
        "tokens",
        "tokens, invite links and signing in",
        (
            "/v1/tokens",
            "/v1/admin/tokens",
            "/v1/admin/invites",
            "/v1/invites",
            r"/v1/auth/(?!whoami$)[^/]+",
            rf"{_PROJECT}/signal-ingest-keys",
        ),
    ),
    (
        "terminal",
        "the worker protocol and the terminal, live view, takeover and handback of a run",
        ("/v1/worker", rf"{_RUN}/(?:terminal|browser|takeover|handback)"),
    ),
    (
        "run access",
        "granting a run its owner's account, its own included, or widening what the owner's runs reach",
        (rf"{_RUN}/access", RUN_REACH_PATH),
    ),
    (WORKERS, "workers, their pools and join tokens", ("/v1/workers", "/v1/worker-pools", "/v1/org/workers")),
)
# (group, why, path patterns): a write of a path in one of them is refused; reading it is not
WRITES_REFUSED = (
    (
        "grants",
        "a grant or a user's admin, which outlive the run",
        ("/v1/admin/projects/[^/]+/grants", "/v1/admin/users"),
    ),
    (
        "autonomy",
        "the owner's autonomy: preferences, stops, policies and the org chart",
        # the org chart's workers are the group workers'
        ("/v1/autonomy", r"/v1/org(?!/workers(?:/|$))", rf"{_PROJECT}/policy"),
    ),
    (AGENTS, "a project's agent profiles and agent types", (rf"{_PROJECT}/agents", rf"{_PROJECT}/agent-types")),
    (
        "runs",
        "starting a run or sending one a message, which would carry the owner's account into work of its own",
        (
            rf"{_PROJECT}/runs$",
            rf"{_PROJECT}/plan-runs",
            rf"{_PROJECT}/author-runs",
            rf"{_RUN}/(?:rerun|messages)",
            rf"{_PROJECT}/local-runs/[^/]+/inbox",
            r"/v1/x/editorial/[^/]+/posts/[^/]+/run",
        ),
    ),
    (
        "decisions",
        "answering a decision of any run or approving work, which is the owner's own judgment",
        (
            rf"{_PROJECT}/decisions/[^/]+/answer",
            rf"{_PROJECT}/local-runs/[^/]+/decisions/[^/]+/answered",
            rf"{_RUN}/approve",
            rf"{_PROJECT}/merge-queue",
            rf"{_PROJECT}/loop/resume",
            rf"{_PROJECT}/goals/[^/]+/baseline/",
            r"/v1/x/builder/[^/]+/proposals",
        ),
    ),
    (
        CURATOR,
        "the Curator's charter, proposals, changes, probes and builds, which judge and change the agents' own work",
        (rf"{_PROJECT}/curator",),
    ),
    (
        RELEASES,
        "a project's release schedule and checking it now, which may queue the cut of a release",
        (rf"{_PROJECT}/releases/(?:run|schedule|resume)",),
    ),
    (
        ANSWERS,
        "asking an answer job of a project's graph, whose batches the hub hands agents as review runs",
        (r"/v1/kg/[^/]+/answers$",),
    ),
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
NEW_RUN = (
    "a run's token (evr_...) acts as its owner but starts no run and hands no agent work, so the owner's account "
    "never passes on into work of its own: the owner does that with their own sign-in"
)
# The run whose token the request being answered carries (``security.Authenticate``); None for any other credential.
ACTING_RUN: ContextVar[int | None] = ContextVar("acting_run", default=None)
MAX_PATH_CHARS = 500  # of the path an audit row names


def _pattern(path: str) -> str:
    """The pattern of one path of a group: that path alone when it ends in ``$``, else it and everything under it."""
    return path[:-1] if path.endswith("$") else path + _UNDER


def _compiled(groups) -> tuple[tuple[str, str, re.Pattern], ...]:
    return tuple(
        (group, why, re.compile("(?:" + "|".join(_pattern(path) for path in patterns) + ")"))
        for group, why, patterns in groups
    )


_REFUSED = _compiled(REFUSED)
_WRITES_REFUSED = _compiled(WRITES_REFUSED)
_REACH = frozenset(REACH_GROUPS)


def reach_order(groups) -> tuple[str, ...]:
    """``groups``, each of REACH_GROUPS, once each and in the order of REACH_GROUPS."""
    given = set(groups)
    return tuple(group for group in REACH_GROUPS if group in given)


def refusal(method: str, path: str, reach: frozenset[str] = frozenset()) -> str | None:
    """Why the token of a run may not make the request ``method`` ``path``, or None when it may. ``reach`` holds the
    groups of REACH_GROUPS its owner lets their runs reach (``users.run_reach``); empty refuses them all, and a name
    outside REACH_GROUPS opens nothing."""
    opened = reach & _REACH
    groups = _REFUSED + (_WRITES_REFUSED if method.upper() in WRITE_METHODS else ())
    for group, why, pattern in groups:
        if group in opened or not pattern.fullmatch(path):
            continue
        refused = f"a run's token (evr_...) acts as its owner but never reaches {why} (group {group}): the owner does "
        if group not in REACH_GROUPS:
            return refused + "that with their own sign-in"
        allowed = ",".join(reach_order(reach | {group}))
        return (
            f"{refused}that with their own sign-in, or lets their runs reach it with `evo-agents hub autonomy "
            f"run-reach --allow {allowed}`"
        )
    return None


def new_run_refusal() -> str | None:
    """Why no run may start now: the request being answered carries a run's token (``ACTING_RUN``); None otherwise."""
    return NEW_RUN if ACTING_RUN.get() is not None else None


@contextmanager
def release_dispatch():
    """While it holds, the request being answered queues runs as though no run's token carried it: the release check
    now (``releases.run_now``), which the middleware lets a run's token reach only when its owner opened RELEASES, and
    whose cut is dispatched as the schedule's owner whoever asked."""
    acting = ACTING_RUN.set(None)
    try:
        yield
    finally:
        ACTING_RUN.reset(acting)


def refuse_new_run() -> None:
    """403 when the request being answered carries a run's token: no run starts from it (``new_run_refusal``)."""
    why = new_run_refusal()
    if why is not None:
        log.warning("a run's token was refused a new run", extra={"run_id": ACTING_RUN.get()})
        raise HTTPException(403, why)


async def record_write(engine: AsyncEngine, principal, method: str, path: str, status: int) -> None:
    """The audit row run_access.write of a write ``principal``, a run's token, made: its run, ``method``, ``path`` and
    the ``status`` it was answered with. A database that fails here leaves a log line, never an error of the request,
    whose answer has gone."""
    from evo_agents.hub.server.runs.service.audits import run_target  # it reads the principal this module guards

    r, p = tables.runs, tables.projects
    shown = path if len(path) <= MAX_PATH_CHARS else path[:MAX_PATH_CHARS] + "..."
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(r.c.project_id, p.c.name, r.c.plan_id, r.c.step_key)
                    .join_from(r, p, p.c.id == r.c.project_id)
                    .where(r.c.id == principal.run_id)
                )
            ).one()
            run = run_target(row.name, row.plan_id, row.step_key, principal.run_id)
            target = f"{run} {method} {shown} {status}"
            await audit.record(
                conn,
                actor_id=principal.user_id,
                token_id=principal.token_id,
                action=audit.RUN_ACCESS_WRITE,
                target=target,
                project_id=row.project_id,
            )
    except Exception as exc:  # the answer went out already; the row is the only thing lost
        log.warning(
            "a write made with a run's token was not audited",
            extra={"run_id": principal.run_id, "method": method, "status": status, "error": type(exc).__name__},
        )
