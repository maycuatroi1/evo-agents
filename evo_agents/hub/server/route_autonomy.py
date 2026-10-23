"""K8 and K12 at the routes (plan autonomy-levels, criterion a6): a write to a route that keeps credentials (secrets,
accounts: K8) or rules (grants, policies, agent profiles, the org and its ceilings: K12), made with the credential of a
run (the agent of a run on /mcp, ``security.RunScope``), reads the mode of the run's principal for the class, the
stricter of the run's snapshot and the value now (``run_autonomy.recheck``).

- deny: 403, naming the class, the mode and its source.
- ask: 403 too, telling the agent to ask its principal (ask_member), who makes the change themself.
- notify and auto: the route goes on and its own rules hold, as before; so does a run without a snapshot.

A caller that is no run's agent, a member or an admin, is never held here: K8 and K12 are what a run may do for its
principal. Every refusal writes the audit row ``autonomy.refused``, and every decision the ledger S1's
(``autonomy_ledger``). Each guard is a dependency of the routes, so it
holds whichever way the route is reached; a read (GET, HEAD, OPTIONS) is never held.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from evo_agents.hub.autonomy_levels import ACTING, ASK, refusal
from evo_agents.hub.server import audit, autonomy_ledger, run_autonomy
from evo_agents.hub.server.autonomy_ledger import ALLOWED, REFUSED
from evo_agents.hub.server.security import CurrentUser, Principal

READS = ("GET", "HEAD", "OPTIONS")
ASK_HINT = "ask your principal with ask_member: they make the change themself, or change your autonomy"


async def held(request: Request, user: Principal, klass: str) -> None:
    """403 when ``klass`` keeps the run of ``user`` from this write; nothing for a caller that is no run's agent."""
    if user.scope is None or request.method in READS:
        return
    route = getattr(request.scope.get("route"), "path", request.url.path)
    engine = request.app.state.engine
    async with engine.begin() as conn:  # the audit row stays: the refusal is raised once it is written
        found = await run_autonomy.recheck(conn, user.scope.run_id)
        choice = None if found is None else found.classes[klass]
        if choice is None:
            return
        act = f"{request.method} {route}"
        result = ALLOWED if choice.mode in ACTING else REFUSED
        await autonomy_ledger.record(conn, user.scope.run_id, klass, choice.mode, result, act, token_id=user.token_id)
        if choice.mode in ACTING:
            return
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.AUTONOMY_REFUSED,
            target=f"run:{user.scope.run_id} {klass} {request.method} {route} mode={choice.mode} "
            f"source={choice.source}",
        )
    why = f"{request.method} {route}: {refusal(klass, choice.mode, choice)}"
    raise HTTPException(403, f"{why}; {ASK_HINT}" if choice.mode == ASK else why)


async def _k8(request: Request, user: CurrentUser) -> None:
    await held(request, user, "K8")


async def _k12(request: Request, user: CurrentUser) -> None:
    await held(request, user, "K12")


K8 = Depends(_k8)  # the routes that write secrets and accounts
K12 = Depends(_k12)  # the routes that write grants, policies, agent profiles, the org and its ceilings
