"""The answer to a credential request (``evo_agents.hub.credential_requests``), which ``decisions.answer_decision``
hands here in its transaction: the value the owner pasted becomes their secret of kind env for the run's project, and
"let the run use my hub account" lets the run run as them (``run_access``), as PUT /v1/secrets and PUT .../access do.

``checked`` reads the answer before anything changes: the value (``AnswerIn.secret``) goes only with an option that
gives it (value or both, which a value with no option stands for), only to a decision that asks for a variable, and
never inside the owner's words; an option that gives it needs it. ``give`` does what the option chose, for the run's
owner alone (403 for anyone else the decision is asked of): the secret is written under the rules of PUT /v1/secrets
(``env_name_refusal``, MAX_SECRET_BYTES, no NUL, MAX_SECRETS_PER_OWNER, the hub's key), and the run is let as by the
access route. Its answer is the sentence the hub adds to the decision's answer: what was given, by name, never the
value.

Which secret keeps the value: the owner's live secret of kind env for that variable bound to the run's project, the one
of the name the request proposes first, else the first by name, whose value is replaced (its name, label and bindings
stay; it ends when its owner says, not before). With none, the secret of the proposed name: replaced when it is the
owner's env secret for that variable (bound to the run's project too, then), 409 when it is a secret of theirs for
something else, and else made, bound to the run's project and, when the run is on a worker of the owner's, to that
worker. Every value written is one audit row secret.put (``secret:<id> decision:<id>``), as PUT writes one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import exists, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import credential_requests, runs, tables
from evo_agents.hub.credentials import HUB_TOKEN_ENV, HUB_URL_ENV, MAX_SECRET_BYTES, MAX_SECRETS_PER_OWNER
from evo_agents.hub.server import audit
from evo_agents.hub.server.account_oauth import drop_tokens
from evo_agents.hub.server.decision_models import AnswerIn
from evo_agents.hub.server.sealing import Sealer, secret_aad
from evo_agents.hub.server.secrets import NO_KEY, _upsert, audit_target, bind_secret, live_secrets
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)

KIND = "env"


@dataclass(frozen=True)
class Asked:
    """An answer to a decision as ``checked`` read it: the option (the one a value stands for when it names none) and
    the value pasted."""

    option: str | None
    value: str | None

    def __repr__(self) -> str:  # the value stays out of tracebacks and logs
        return f"Asked(option={self.option!r}, value={'***' if self.value is not None else None})"


def checked(decision_id: int, request: dict | None, body: AnswerIn) -> Asked:
    """The option and value of ``body``, an answer to decision ``decision_id``, whose credential request is
    ``request`` (None for any other decision); 422 for a value where none goes and for an option that gives one
    without it. No message holds the value."""
    value = None if body.secret is None else body.secret.get_secret_value()
    option = body.option
    env_var = None if request is None else request.get("env_var")
    if value is None:
        if request is not None and credential_requests.gives_value(option):
            raise HTTPException(
                422,
                f"option {option} of decision {decision_id} gives the value of {env_var}: paste it with "
                f"`evo-agents hub decision answer {decision_id} --secret`, or in the password field on the web",
            )
        return Asked(option, None)
    if env_var is None:
        raise HTTPException(422, f"decision {decision_id} asks for no variable's value: answer it without one")
    option = option or credential_requests.VALUE
    if not credential_requests.gives_value(option):
        raise HTTPException(
            422,
            f"option {option} of decision {decision_id} takes no value: send the value with option "
            f"{credential_requests.VALUE} or {credential_requests.BOTH}, or answer without it",
        )
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise HTTPException(422, f"a secret's value is at most {MAX_SECRET_BYTES} bytes of UTF-8")
    if "\x00" in value:
        raise HTTPException(422, "a secret's value may not hold a NUL character")
    if body.text is not None and value in body.text:
        raise HTTPException(
            422, "your words hold the value you pasted, and they go to the agent: answer again without it in them"
        )
    return Asked(option, value)


def acts(request: dict | None, asked: Asked) -> bool:
    """Whether the answer gives the run something: a value, or the owner's account on the hub."""
    return request is not None and (asked.value is not None or credential_requests.grants_account(asked.option))


def with_sentence(words: str | None, sentence: str) -> str:
    """The decision's answer text: the owner's words, then the hub's sentence; 422 past MAX_ANSWER_BYTES."""
    text = sentence if words is None else f"{words.strip()}\n\n{sentence}"
    if len(text.encode()) > runs.MAX_ANSWER_BYTES:
        room = runs.MAX_ANSWER_BYTES - len(sentence.encode()) - 2
        raise HTTPException(
            422, f"your words and what the hub adds to them pass {runs.MAX_ANSWER_BYTES} bytes: keep them to {room}"
        )
    return text


async def _owner(conn: AsyncConnection, user: Principal, run_id: int, decision_id: int) -> None:
    """403 unless ``user`` dispatched run ``run_id``: a credential is the owner's to give, and so is their account."""
    r, u = tables.runs, tables.users
    found = select(r.c.dispatched_by, u.c.login).join_from(r, u, u.c.id == r.c.dispatched_by).where(r.c.id == run_id)
    owner_id, login = (await conn.execute(found)).one()
    if owner_id != user.user_id:
        raise HTTPException(
            403,
            f"only {login}, who dispatched run {run_id}, gives it a credential or their account: answer decision "
            f"{decision_id} with option {credential_requests.REFUSE}, or with words alone",
        )


async def _own_worker(conn: AsyncConnection, user: Principal, run_id: int) -> int | None:
    """The worker run ``run_id`` is on (or was parked on), when it is one of ``user``'s that is not revoked."""
    r, w = tables.runs, tables.workers
    found = (
        select(w.c.id)
        .join_from(r, w, w.c.id == r.c.worker_id)
        .where(r.c.id == run_id, w.c.owner_id == user.user_id, w.c.revoked_at.is_(None))
    )
    return (await conn.execute(found)).scalar_one_or_none()


@dataclass(frozen=True)
class Kept:
    name: str
    created: bool


async def keep_value(
    conn: AsyncConnection,
    sealer: Sealer,
    user: Principal,
    project_id: int,
    run_id: int,
    decision_id: int,
    request: dict,
    value: str,
) -> Kept:
    """Keep ``value`` as ``user``'s secret of kind env for the request's variable, bound to the run's project (see the
    module's docstring for which one); one audit row secret.put."""
    env_var = request["env_var"]
    proposed = request.get("secret_name") or credential_requests.default_secret_name(env_var)
    users, s, b = tables.users, tables.secrets, tables.secret_bindings
    # One write of a member's secrets at a time, as PUT /v1/secrets takes them.
    await conn.execute(select(users.c.id).where(users.c.id == user.user_id).with_for_update())
    live = (s.c.owner_id == user.user_id, s.c.deleted_at.is_(None))
    bound_here = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    same = (
        select(s.c.name, s.c.environment)
        .where(*live, s.c.kind == KIND, s.c.env_var == env_var, bound_here)
        .order_by(s.c.name != proposed, s.c.name)
        .limit(1)
    )
    found = (await conn.execute(same)).one_or_none()
    bind = False
    if found is not None:
        name, environment = found.name, found.environment
    else:
        named = select(s.c.kind, s.c.env_var, s.c.environment).where(*live, s.c.name == proposed)
        held = (await conn.execute(named)).one_or_none()
        if held is not None and (held.kind != KIND or held.env_var != env_var):
            raise HTTPException(
                409,
                f"your secret {proposed} is for something else than {env_var}: rename or delete it, or keep the value "
                f"with `evo-agents hub secret set` under another name, then answer decision {decision_id} without it",
            )
        if held is None and await live_secrets(conn, user.user_id, proposed) >= MAX_SECRETS_PER_OWNER:
            raise HTTPException(
                409, f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another"
            )
        name, environment, bind = proposed, None if held is None else held.environment, True
    sealed = sealer.seal(value, secret_aad(user.user_id, name, KIND))
    statement = _upsert(
        user.user_id,
        name,
        kind=KIND,
        env_var=env_var,
        url_prefix=None,
        username=None,
        environment=environment,
        sealed=sealed.ciphertext,
        nonce=sealed.nonce,
        key_id=sealed.key_id,
        expires_at=None,
    )
    secret_id, created = (await conn.execute(statement)).one()
    if bind:
        worker_id = await _own_worker(conn, user, run_id)
        if created:
            await bind_secret(conn, secret_id, [project_id], [worker_id] if worker_id is not None else [])
        else:  # the owner's secret for this variable, bound elsewhere: this project too, its other bindings kept
            row = {"secret_id": secret_id, "project_id": project_id, "worker_id": worker_id, "pool_id": None}
            await conn.execute(insert(b).values(row))
    await drop_tokens(conn, secret_id)  # written whole: no OAuth tokens of the value it replaced
    target = f"{audit_target(secret_id)} decision:{decision_id}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_PUT, target=target)
    extra = {"secret": name, "kind": KIND, "login": user.login, "replaced": not created, "decision_id": decision_id}
    log.info("secret written from a decision", extra=extra | {"key_id": sealed.key_id})
    return Kept(name, created)


async def give(
    conn: AsyncConnection,
    user: Principal,
    sealer: Sealer | None,
    access,
    project: str,
    decision_id: int,
    run_id: int,
    request: dict,
    asked: Asked,
) -> str:
    """Do what ``asked`` chose of credential request ``request`` of decision ``decision_id``, in the caller's
    transaction, for the owner of run ``run_id`` alone; the sentence the decision's answer carries, without the
    value."""
    await _owner(conn, user, run_id, decision_id)
    said = []
    if asked.value is not None:
        if sealer is None:
            raise HTTPException(503, NO_KEY.format(missing="EVO_HUB_SECRETS_KEY"))
        kept = await keep_value(conn, sealer, user, access.project_id, run_id, decision_id, request, asked.value)
        how = "a new secret" if kept.created else "the value it held replaced"
        said.append(
            f"{user.login} gave {request['env_var']}: the hub keeps it as their secret {kept.name} of kind env for "
            f"project {project} ({how}), and leases it to the run."
        )
    if credential_requests.grants_account(asked.option):
        from evo_agents.hub.server.runs.service.controls import let_run_as_owner  # it imports the routes of runs

        if await let_run_as_owner(conn, user, access, project, run_id, f"by=decision:{decision_id}"):
            said.append(
                f"{user.login} lets the run run as them on the hub, secrets, tokens and workers aside: "
                f"{HUB_TOKEN_ENV} and {HUB_URL_ENV} come with its leases."
            )
        else:
            said.append(f"The run runs as {user.login} on the hub already.")
    said.append(
        f"The worker takes the run's leases again: read them with `{credential_requests.ENV_COMMAND}` before the "
        "command that needs them (a run that resumes has them in its environment from its start)."
    )
    return " ".join(said)
