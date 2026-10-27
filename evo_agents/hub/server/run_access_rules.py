"""What the token of a run that runs as its owner never reaches, what starts no run while it is answered, and the
audit row of each write made with it (``evo_agents.hub.server.run_access``).

A run's token (``security.RUN``, ``evr_...``) is its owner with the owner's grants on every project, and hub admin when
the owner is one; ``Authenticate`` refuses it, with 403 and the reason, every path of the groups of REFUSED, whatever
the method, and every write (POST, PUT, PATCH, DELETE) of the groups of WRITES_REFUSED. A group is a closed list of
path patterns, each matching its prefix and everything under it (a pattern that ends in ``$`` matches that path
alone), so a route added later under a group's prefix is refused with the group until someone takes it out of the
group: secrets and everything that holds a credential's value, tokens and signing in (whoami aside), workers with the
terminal and the live view of a run, and the run access of any run, its own included, so no run grants running as its
owner onward; and no write of grants and users, of the owner's autonomy (preferences, stops, policies, the org chart,
agent profiles and types), no run started or messaged, no decision answered and no approval given, and nothing of the
Curator's. What the agent of a run never does on a route open to it is the route's to refuse, as for the agent of a run
on /mcp: each of those rules tests ``Principal.acts_for_run``, which both have.

While the middleware answers a request of a run's token, ACTING_RUN holds the run's id, and no run is queued whatever
route the request took: ``refuse_new_run`` is the last word of every place that inserts a run
(``runs.service.dispatch.insert_run``, the session of a chat run and a parked run's resumption), and
``new_run_refusal`` the reason a place that refuses with words (a mention in a chat, a core plugin's task) says.

``record_write`` adds the audit row run_access.write of each write made with a run's token, refused or not, once its
answer started: the run, the method, the path and the status, never a query string, a body or a value; the actor is the
owner and the token the run's.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.server import audit

log = logging.getLogger(__name__)

_UNDER = r"(?:/.*)?"  # the prefix itself and every path under it
_PROJECT = r"/v1/projects/[^/]+"
_RUN = rf"{_PROJECT}/runs/[^/]+"
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
        "workers",
        "workers, their pools, the terminal and the live view of a run",
        (
            "/v1/workers",
            "/v1/worker-pools",
            "/v1/org/workers",
            "/v1/worker",
            rf"{_RUN}/(?:terminal|browser|takeover|handback)",
        ),
    ),
    ("run access", "granting a run its owner's account, its own included", (rf"{_RUN}/access",)),
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
        "the owner's autonomy: preferences, stops, policies, the org chart and agent profiles",
        ("/v1/autonomy", "/v1/org", rf"{_PROJECT}/policy", rf"{_PROJECT}/agents", rf"{_PROJECT}/agent-types"),
    ),
    (
        "runs",
        "starting a run or sending one a message, which would carry the owner's account into work of its own",
        (
            rf"{_PROJECT}/runs$",
            rf"{_PROJECT}/plan-runs",
            rf"{_PROJECT}/author-runs",
            rf"{_RUN}/(?:rerun|messages)",
            rf"{_PROJECT}/local-runs/[^/]+/inbox",
            rf"{_PROJECT}/releases/(?:run|schedule|resume)",
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
            r"/v1/x/builder/[^/]+/proposals",
        ),
    ),
    (
        "curator",
        "the Curator's charter, proposals, changes, probes and builds, which judge and change the agents' own work",
        (rf"{_PROJECT}/curator",),
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


def refusal(method: str, path: str) -> str | None:
    """Why the token of a run may not make the request ``method`` ``path``, or None when it may."""
    groups = _REFUSED + (_WRITES_REFUSED if method.upper() in WRITE_METHODS else ())
    for group, why, pattern in groups:
        if pattern.fullmatch(path):
            return (
                f"a run's token (evr_...) acts as its owner but never reaches {why} (group {group}): the owner does "
                "that with their own sign-in"
            )
    return None


def new_run_refusal() -> str | None:
    """Why no run may start now: the request being answered carries a run's token (``ACTING_RUN``); None otherwise."""
    return NEW_RUN if ACTING_RUN.get() is not None else None


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
