"""What the hub finds for a credential request (``evo_agents.hub.credential_requests``) before it asks anyone: the run's
owner may hold the credential already (plan hub-attention-runtime, criterion a2; ``docs/credentials.md``, "A run that
asks for a credential").

``look`` reads, for a request that names a variable, the owner's live secrets of kind env that set it, and what the
lease of the run (``survey.survey_run``: its project, its worker, its agent profile, the Curator's charter, K7 of
its principal) makes of each:

- ``leased``: the run is leased it now, so the hub answers the request itself (``decisions.ask``): the decision is
  answered by nobody (``answered_by`` NULL, as a wait the hub wakes), with the option FOUND naming the secret, its answer
  goes to the run's inbox, so the worker takes the run's leases again as when the owner answers, and the owner gets a
  notice of kind ``autonomy`` instead of a decision in their Inbox;
- ``bindable``: it is not bound to the run's project on the run's worker, and nothing else keeps it from the run. When
  K12 of the run's principal is auto (``k12``) the hub binds it there (``bind``, the audit row ``secret.bind``)
  and answers as above; else the decision goes to the owner, offering FOUND, which binds it when they choose it
  (``answers.give``);
- ``withheld``: the rest, which the run is not leased whatever it is bound to: the decision's context names them.

A request that also asks for the owner's hub account is never answered by the hub: the account is the owner's to give.
Nothing here reads a secret's value: ``look`` and ``bind`` read and write names, ids and bindings alone (criterion a6).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import credential_requests, runs
from evo_agents.hub.autonomy_levels import Choice
from evo_agents.hub.server import audit
from evo_agents.hub.server.autonomy.service import lease as lease_autonomy
from evo_agents.hub.server.autonomy.service import ledger as autonomy_ledger
from evo_agents.hub.server.autonomy.service import runs as run_autonomy
from evo_agents.hub.server.autonomy.service.ledger import ALLOWED
from evo_agents.hub.server.credentials.models.answers import KIND
from evo_agents.hub.server.credentials.models.resolver import K12, Found
from evo_agents.hub.server.credentials.repository import answers as answer_repository
from evo_agents.hub.server.credentials.repository import resolver as repository
from evo_agents.hub.server.credentials.service.secrets import audit_target
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


def _preferred(proposed: str):
    return lambda name: (name != proposed, name)


def _proposed(request: dict) -> str:
    return request.get("secret_name") or credential_requests.default_secret_name(request["env_var"])


async def look(conn: AsyncConnection, user: Principal, run_id: int, request: dict | None) -> Found | None:
    """What the hub finds for credential request ``request`` of run ``run_id``, which the worker of ``user`` holds;
    None for a request that names no variable, and when the run would be leased nothing at all (its owner lost the
    writer role, say), which the decision then tells its owner as before."""
    if request is None or request.get("env_var") is None:
        return None
    from evo_agents.hub.server.credentials.service import survey as lease_survey  # it reads runs through routes

    try:
        worker_id, survey = await lease_survey.survey_run(conn, user, run_id, lock=False)
    except HTTPException as exc:
        log.info("credential request not looked up", extra={"run_id": run_id, "status": exc.status_code})
        return None
    run, env_var = survey.run, request["env_var"]
    order = _preferred(_proposed(request))
    found = Found(env_var, run.project)
    found.leased = sorted(
        {
            secret.name
            for secret in survey.secrets
            if secret.kind == KIND
            and secret.env_var == env_var
            and secret.owner_id == run.owner_id
            and secret.share_id is None
            and secret.agent is None
            and secret.pool is None
        },
        key=order,
    )
    rows = await repository.env_secrets(conn, run.owner_id, env_var, run.project_id, worker_id)
    allowed = lease_survey.allowed_secrets(run.curator)
    for row in sorted(rows, key=lambda row: order(row.name)):
        if row.name in found.leased:
            continue
        takes = lease_survey.leasable(run, allowed, row) and lease_autonomy.secret_allowed(
            survey.k7, *lease_survey.row_k7(row)
        )
        if takes and not row.bound:
            found.bindable.append((row.id, row.name))
        else:
            found.withheld.append(row.name)
    return found


def answerable(request: dict) -> bool:
    """Whether the hub may answer ``request`` itself: it asks for a variable alone, not the owner's account."""
    return request.get("env_var") is not None and not request.get("hub_account")


async def k12(conn: AsyncConnection, run_id: int) -> Choice | None:
    """K12 of the principal of run ``run_id`` now (``run_autonomy.recheck``); None for a run without an autonomy
    snapshot. The hub changes a binding for the run only when its mode is AUTO."""
    reached = await run_autonomy.recheck(conn, run_id)
    return None if reached is None else reached.classes[K12]


async def _run_worker(conn: AsyncConnection, owner_id: int, run_id: int) -> int | None:
    """The worker run ``run_id`` is on, when it is one of its owner's that is not revoked: a binding names it, as the
    answer of a pasted value does (``answers.keep_value``); else None, any worker of the owner."""
    return await answer_repository.own_worker(conn, owner_id, run_id)


async def bind(
    conn: AsyncConnection,
    *,
    secret_id: int,
    owner_id: int,
    project_id: int,
    run_id: int,
    decision_id: int,
    by: str,
    actor: Principal | None = None,
) -> None:
    """Bind secret ``secret_id`` of ``owner_id`` to project ``project_id`` on the worker of run ``run_id`` (its other
    bindings kept), for decision ``decision_id``: one audit row secret.bind naming the secret by id, the run, the
    decision and ``by`` (hub, or the owner's decision), never its name or value."""
    worker_id = await _run_worker(conn, owner_id, run_id)
    await repository.add_binding(conn, secret_id, project_id, worker_id)
    target = f"{audit_target(secret_id)} run:{run_id} decision:{decision_id} by={by}"
    await audit.record(
        conn,
        actor_id=None if actor is None else actor.user_id,
        token_id=None if actor is None else actor.token_id,
        action=audit.SECRET_BIND,
        target=target,
        project_id=project_id,
    )
    log.info("secret bound for a credential request", extra={"run_id": run_id, "decision_id": decision_id, "by": by})


async def bind_by_hub(conn: AsyncConnection, row, run_id: int, decision_id: int, found: Found, choice: Choice) -> str:
    """Bind the first bindable secret of ``found`` for run ``run_id`` (whose row ``row`` is), as K12 ``choice`` of its
    principal lets the hub: the ledger of autonomy keeps the act. The secret's name."""
    secret_id, name = found.bindable[0]
    await bind(
        conn,
        secret_id=secret_id,
        owner_id=row.dispatched_by,
        project_id=row.project_id,
        run_id=run_id,
        decision_id=decision_id,
        by="hub",
    )
    act = f"bind {audit_target(secret_id)} to project {row.name} for decision:{decision_id}"
    await autonomy_ledger.record(conn, run_id, K12, choice.mode, ALLOWED, act)
    return name


async def bind_chosen(
    conn: AsyncConnection, user: Principal, project_id: int, run_id: int, decision_id: int, request: dict
) -> str:
    """The owner chose FOUND: bind their first secret for the request's variable that is not bound to the project on
    the run's worker yet, the one the request proposes first (``bind``); its name. 409 when they hold none for it any
    more."""
    env_var = request["env_var"]
    rows = await repository.chosen_secrets(conn, user.user_id, env_var, project_id, run_id)
    if not rows:
        raise HTTPException(
            409,
            f"you hold no secret for {env_var} any more: paste its value with `evo-agents hub decision answer "
            f"{decision_id} --secret`",
        )
    order = _preferred(_proposed(request))
    rows = sorted(rows, key=lambda row: (row.bound, order(row.name)))
    chosen = rows[0]
    if not chosen.bound:
        await bind(
            conn,
            secret_id=chosen.id,
            owner_id=user.user_id,
            project_id=project_id,
            run_id=run_id,
            decision_id=decision_id,
            by="owner",
            actor=user,
        )
    return chosen.name


def found_option(found: Found, *, leased: bool) -> dict:
    """The option FOUND of a request ``found`` answers: the secret it names, leased to the run already (``leased``) or
    bound for it first."""
    name = found.leased[0] if leased else found.bindable[0][1]
    if leased:
        description = (
            f"Your secret {name} of kind env sets {found.env_var} and is bound to project {found.project}: the run is "
            "leased it, and reads it at once."
        )
    else:
        description = (
            f"Your secret {name} of kind env sets {found.env_var}: the hub binds it to project {found.project} on the "
            "run's worker, its other bindings kept, and the run reads it at once. Nothing is pasted."
        )
    return {"key": credential_requests.FOUND, "label": f"Lease my secret {name}", "description": description}


def answer_sentence(found: Found, *, bound: bool) -> str:
    """The hub's answer of a request it answers itself, by the secret's name, never its value."""
    name = found.leased[0]
    how = f"bound it to project {found.project} for the run, as K12 lets it, and " if bound else ""
    return (
        f"The hub answered: {found.env_var} is set by your secret {name} of kind env, and the hub {how}leases it to the "
        f"run. The worker takes the run's leases again: read them with `{credential_requests.ENV_COMMAND}` before the "
        "command that needs them."
    )


def inbox_context(found: Found, decision_id: int, mode: str | None) -> str:
    """What the decision's context adds when the hub found the owner's secret but did not answer itself: which, and the
    command that binds it."""
    lines = ["**What the hub found**", ""]
    if found.leased:
        lines.append(
            f"- The run is leased your secret {', '.join(found.leased)} for {found.env_var} already; the request asks "
            "for your hub account too, which only you give."
        )
    if found.bindable:
        names = ", ".join(name for _, name in found.bindable)
        why = f"K12 of the run's principal is {mode}" if mode else "the run has no autonomy snapshot"
        lines.append(
            f"- Your secret {names} of kind env sets {found.env_var} but is not bound to project {found.project} on "
            f"the run's worker, and {why}, so the hub did not bind it. Bind it and lease it to the run with "
            f"`evo-agents hub decision answer {decision_id} --option {credential_requests.FOUND} --project "
            f"{found.project}`."
        )
    if found.withheld:
        lines.append(
            f"- Your secret {', '.join(found.withheld)} sets {found.env_var}, but the run is not leased it: its agent "
            "profile, the Curator's charter or K7 of its principal keeps it from the run."
        )
    return "\n".join(lines)


def with_context(context: str | None, added: str) -> str:
    text = added if context is None else f"{context.rstrip()}\n\n{added}"
    return runs.clip(text, runs.MAX_DECISION_CONTEXT_BYTES)
