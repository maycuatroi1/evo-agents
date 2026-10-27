"""What the token of a run that runs as its owner never reaches, and the audit row of each write made with it
(``evo_agents.hub.server.run_access``).

A run's token (``security.RUN``, ``evr_...``) is its owner with the owner's grants on every project, and hub admin when
the owner is one; ``Authenticate`` refuses it, with 403 and the reason, every path of the groups of REFUSED, whatever
the method, and every write (POST, PUT, PATCH, DELETE) of the groups of WRITES_REFUSED. A group is a closed list of
path patterns, each matching its prefix and everything under it, so a route added later under a group's prefix is
refused with the group until someone takes it out of the group: secrets and everything that holds a credential's value,
tokens and signing in (whoami aside), workers with the terminal and the live view of a run, and the run access of any
run, its own included, so no run grants running as its owner onward; and no write of the owner's autonomy. What the
run may not do on a route that is open to it is the route's to refuse: no answer to a decision of its own run
(``decisions.answer_decision``), no ``as_me`` on a dispatch, a plan run or a rerun (``run_access.refuse_onward``).

``record_write`` adds the audit row run_access.write of each write made with a run's token, refused or not, once its
answer started: the run, the method, the path and the status, never a query string, a body or a value; the actor is the
owner and the token the run's.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import tables
from evo_agents.hub.server import audit

log = logging.getLogger(__name__)

_UNDER = r"(?:/.*)?"  # the prefix itself and every path under it
_RUN = r"/v1/projects/[^/]+/runs/[^/]+"
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
            r"/v1/projects/[^/]+/agents/[^/]+/(?:accounts|provider-accounts)",
            r"/v1/projects/[^/]+/browser-identities",
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
            r"/v1/projects/[^/]+/signal-ingest-keys",
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
        "autonomy",
        "the owner's autonomy",
        ("/v1/autonomy", "/v1/org/ceilings", r"/v1/projects/[^/]+/policy"),
    ),
)
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_PATH_CHARS = 500  # of the path an audit row names


def _compiled(groups) -> tuple[tuple[str, str, re.Pattern], ...]:
    return tuple((group, why, re.compile("(?:" + "|".join(patterns) + ")" + _UNDER)) for group, why, patterns in groups)


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
