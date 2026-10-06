"""The leases of a run: what the worker holding a run gets of its owner's secrets and of the hub's GitHub App, and how
the run gives them back (``docs/credentials.md``).

POST /v1/worker/runs/{id}/credentials, with the worker's token and the protocol header as on every worker route, asks
for the leases of a run the worker holds, in one of ``runs.HELD_STATES`` (``waiting`` of a plan run among them); any
other run, another worker's or one that ended, is 404 as for a state report. The hub takes the run's repos (its repo
for a run of one step, its repos for a plan run) and their origins in project_repos, compared in the form of
``credentials.normalize_origin``, and leases what the run's owner, the member who dispatched it, bound to the run's
project, on any of their workers or on this one, and that has not expired:

- each secret of kind env, one per variable: a secret bound to this worker before one bound to any, then by name;
- each secret of kind git whose url_prefix covers one of the origins, the longest prefix for each origin, so a
  secret for ``https://gitlab.example.org/group`` answers for ``git@gitlab.example.org:group/repo.git`` too;
- a token of the hub's GitHub App for the origins on github.com that no git secret covers, one per installation
  (``GitHubApp.tokens``): a lease named ``github-app:<account>`` of kind git for ``https://github.com/<owner>``, with
  GITHUB_GIT_USERNAME.

The answer is {leases, missing}: each lease as ``evo_agents.hub.credentials.Lease.to_json`` writes it, value included,
and for each of the run's repos whose origin nothing covers, the repo, its origin and why: no origin registered, no
secret covering it, the App not installed on it or not configured, GitHub failing. Without EVO_HUB_SECRETS_KEY nothing
is leased and every repo is missing, with that variable as the reason. A secret is leased once per run and worker:
asked again, the same lease comes back with the secret's value as it is now. A GitHub token is kept sealed in its lease
(``sealing.lease_aad``) until it expires, so the hub can still revoke it. Asked again, the run gets the same tokens
while each has GITHUB_TOKEN_REFRESH_SECONDS or more left, and new ones, in new leases, after that; the tokens they
replace go on working until they expire, so a push that took one is not cut off. GitHub is asked between two
transactions, never while the run's row is locked; the second one checks the run is still held, and when it is not,
the tokens just made are revoked and the answer is 404. Every ask adds one audit row credential.lease naming the run,
the secrets, the App's accounts and the repos, never a value.

DELETE on the same route gives back every lease this worker holds of the run, whatever state the run is in now: the
leases are marked revoked, each GitHub token is revoked with itself (DELETE /installation/token) and its sealed value
dropped, and one audit row credential.revoke names the run and what it gave back. A run of another worker is 404.

The hub gives leases back itself (``end_leases``) when a run leaves the held states (``run_state.move_run``: it ends,
waits in review, or is parked) and when a worker is revoked (``workers.end_worker``, every lease of the worker). Those
rows are marked revoked in the transaction of the move, and their GitHub tokens revoked at GitHub by ``revoke_tokens``
once it commits: by the route that made the move, and on every pass of the reaper (``run_state.recover_runs``), which
also takes the tokens GitHub failed to answer for. ``drop_expired`` (from the daily ``run_state.prune_run_events``)
drops the sealed values of tokens past their end, which GitHub no longer takes. No log line, audit row or error holds a
value.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from evo_agents.hub import runs
from evo_agents.hub.credentials import (
    GITHUB_HOST,
    GITHUB_TOKEN_REFRESH_SECONDS,
    PROVIDERS,
    SECRET_KINDS,
    Lease,
    github_repo,
    matches,
    normalize_origin,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, InstallationToken
from evo_agents.hub.server.runs import NOT_HELD, RunId, _run_target, _worker_of
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad, secret_aad
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

GITHUB_GIT_USERNAME = "x-access-token"  # the user git sends with an installation token, as GitHub documents it
MAX_TARGET_CHARS = 2000  # credential_leases.target
REVOKE_BATCH = 500  # GitHub tokens one pass of revoke_tokens takes
REFRESH = timedelta(seconds=GITHUB_TOKEN_REFRESH_SECONDS)
NO_KEY = "this hub keeps no secrets and leases nothing: it needs {missing} (docs/credentials.md)"
NO_APP = "no git secret covers it, and this hub has no GitHub App: {missing} not set (docs/credentials.md)"

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


# Models


class CredentialLease(BaseModel):
    """One credential a run holds, as ``evo_agents.hub.credentials.Lease`` reads it."""

    id: int = Field(description="the lease; the same id when it is asked for again")
    kind: Literal[SECRET_KINDS] = Field(description="env: a variable of the agent; git: what git's helper answers")
    provider: Literal[PROVIDERS] = Field(description="secret: the owner's; github-app: a token the hub's App made")
    name: str = Field(description="the secret's name, or github-app:<account>")
    env_var: str | None = Field(description="kind env: the variable it sets")
    url_prefix: str | None = Field(description="kind git: the https prefix of the origins it answers for")
    username: str | None = Field(description="kind git: the user git sends with the value")
    value: str = Field(description="the credential itself: for memory only, never a file, a log line or an event")
    expires_at: datetime | None = Field(description="when it stops working; ask again before, for a GitHub token")


class MissingOrigin(BaseModel):
    """A repo of the run whose origin no lease covers: git uses the machine's own credentials for it."""

    repo: str
    origin: str | None = Field(description="as the project registered it; null when it registered none")
    reason: str


class Credentials(BaseModel):
    leases: list[CredentialLease]
    missing: list[MissingOrigin]


class GivenBack(BaseModel):
    revoked: int = Field(description="leases of the run this worker gave back now; 0 when none was out any more")


# What a run may get


@dataclass(frozen=True)
class _Run:
    id: int
    state: str
    worker_id: int | None
    kind: str
    project_id: int
    project: str
    plan_id: str
    step_key: str | None
    owner_id: int
    owner: str
    repos: tuple[str, ...]

    @property
    def target(self) -> str:
        return _run_target(self.project, self.plan_id, self.step_key, self.id)


@dataclass(frozen=True)
class _Secret:
    id: int
    name: str
    kind: str
    env_var: str | None
    url_prefix: str | None
    username: str | None
    sealed: Sealed
    expires_at: datetime | None
    on_worker: bool  # bound to this worker, not to any worker of its owner


@dataclass(frozen=True)
class _HeldToken:
    """A GitHub token the run holds already, with GITHUB_TOKEN_REFRESH_SECONDS or more left."""

    id: int
    origins: frozenset[str]  # what it answers for, normalized
    expires_at: datetime
    sealed: Sealed


@dataclass
class _Survey:
    run: _Run
    origins: dict[str, str | None]  # repo -> origin as registered, in the run's order
    secrets: list[_Secret]
    tokens: list[_HeldToken]
    env: list[_Secret] = field(default_factory=list)
    git: dict[str, _Secret] = field(default_factory=dict)  # repo -> the secret that answers for its origin
    github: dict[str, tuple[str, str]] = field(default_factory=dict)  # repo -> (owner, repo) on github.com
    missing: dict[str, str] = field(default_factory=dict)  # repo -> why nothing covers its origin

    def reusable(self) -> bool:
        """Whether the GitHub tokens the run holds cover every repo that needs one."""
        held = set().union(*(token.origins for token in self.tokens)) if self.tokens else set()
        return all(normalize_origin(self.origins[repo]) in held for repo in self.github)


RUN = """
SELECT r.state, r.worker_id, r.kind, r.project_id, p.name, r.plan_id, r.step_key, r.dispatched_by, u.login, r.repo,
       r.repos
  FROM runs r JOIN projects p ON p.id = r.project_id JOIN users u ON u.id = r.dispatched_by
 WHERE r.id = %s
"""
ORIGINS = "SELECT name, origin FROM project_repos WHERE project_id = %s AND name = ANY(%s)"
# The owner's secrets bound to the run's project, on any of their workers or on this one, live and not past their end.
SECRETS = """
SELECT s.id, s.name, s.kind, s.env_var, s.url_prefix, s.username, s.sealed, s.nonce, s.key_id, s.expires_at,
       EXISTS (SELECT 1 FROM secret_bindings b
                WHERE b.secret_id = s.id AND b.project_id = %(project)s AND b.worker_id = %(worker)s)
  FROM secrets s
 WHERE s.owner_id = %(owner)s AND s.deleted_at IS NULL AND (s.expires_at IS NULL OR s.expires_at > now())
   AND EXISTS (SELECT 1 FROM secret_bindings b
                WHERE b.secret_id = s.id AND b.project_id = %(project)s
                  AND (b.worker_id IS NULL OR b.worker_id = %(worker)s))
 ORDER BY s.name
"""
HELD_TOKENS = """
SELECT id, target, expires_at, sealed_value, nonce, key_id
  FROM credential_leases
 WHERE run_id = %(run)s AND worker_id = %(worker)s AND provider = 'github-app' AND revoked_at IS NULL
   AND sealed_value IS NOT NULL AND expires_at > now() + %(refresh)s
 ORDER BY id DESC
"""
LIVE_SECRET_LEASES = """
SELECT secret_id, target, id FROM credential_leases
 WHERE run_id = %s AND worker_id = %s AND provider = 'secret' AND revoked_at IS NULL
"""
INSERT_LEASE = """
INSERT INTO credential_leases (run_id, worker_id, secret_id, provider, target, external_id, expires_at)
VALUES (%(run)s, %(worker)s, %(secret)s, %(provider)s, %(target)s, %(external)s, %(expires_at)s)
RETURNING id
"""
SEAL_LEASE = "UPDATE credential_leases SET sealed_value = %s, nonce = %s, key_id = %s WHERE id = %s"


def _run_repos(kind: str, repo: str | None, repos) -> tuple[str, ...]:
    if kind == "plan":
        names = [entry.get("repo") for entry in repos or [] if isinstance(entry, dict)]
    else:
        names = [repo]
    return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name))


async def _held_run(conn, worker_id: int, run_id: int, *, lock: bool) -> _Run:
    """The run ``run_id`` that worker ``worker_id`` holds, its row locked with ``lock``; 404 for any other."""
    row = await (await conn.execute(RUN + ("   FOR UPDATE OF r" if lock else ""), (run_id,))).fetchone()
    if row is None or row[1] != worker_id or row[0] not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    state, worker, kind, project_id, project, plan_id, step_key, owner_id, owner, repo, repos = row
    names = _run_repos(kind, repo, repos)
    return _Run(run_id, state, worker, kind, project_id, project, plan_id, step_key, owner_id, owner, names)


def _origins_text(origins) -> str:
    """The origins a lease answers for, as credential_leases.target keeps them: space-separated, within its limit."""
    kept: list[str] = []
    for origin in sorted(set(origins)):
        if len(" ".join([*kept, origin])) > MAX_TARGET_CHARS:
            break
        kept.append(origin)
    return " ".join(kept) or "-"


async def _survey(conn, user: Principal, run_id: int, *, lock: bool) -> tuple[int, _Survey]:
    """(worker id, what run ``run_id`` may get) for the worker of ``user``, which must hold the run. The worker's row
    is locked, as on every worker route; with ``lock`` the run's and the secrets' rows are too, so the run cannot end
    and a secret cannot be deleted until the leases are recorded."""
    worker_id = (await _worker_of(conn, user))[0]
    run = await _held_run(conn, worker_id, run_id, lock=lock)
    registered = dict(await (await conn.execute(ORIGINS, (run.project_id, list(run.repos)))).fetchall())
    params = {"owner": run.owner_id, "project": run.project_id, "worker": worker_id}
    rows = await (await conn.execute(SECRETS + (" FOR SHARE OF s" if lock else ""), params)).fetchall()
    secrets = [
        _Secret(row[0], row[1], row[2], row[3], row[4], row[5], Sealed(row[6], row[7], row[8]), row[9], row[10])
        for row in rows
    ]
    params = {"run": run_id, "worker": worker_id, "refresh": REFRESH}
    tokens = [
        _HeldToken(row[0], frozenset(row[1].split()), row[2], Sealed(row[3], row[4], row[5]))
        for row in await (await conn.execute(HELD_TOKENS, params)).fetchall()
    ]
    survey = _Survey(run, {repo: registered.get(repo) for repo in run.repos}, secrets, tokens)
    _choose(survey)
    return worker_id, survey


def _choose(survey: _Survey) -> None:
    """Which env secrets, which git secret for each origin, and which repos need a GitHub token; the reason of each
    repo that nothing can cover."""
    by_preference = sorted(survey.secrets, key=lambda secret: (not secret.on_worker, secret.name))
    variables: dict[str, _Secret] = {}
    for secret in by_preference:
        if secret.kind == "env":
            variables.setdefault(secret.env_var, secret)
    survey.env = sorted(variables.values(), key=lambda secret: secret.env_var)
    git = [secret for secret in by_preference if secret.kind == "git"]
    for repo, origin in survey.origins.items():
        if not origin:
            survey.missing[repo] = f"project {survey.run.project} lists no origin for {repo}"
            continue
        covering = [secret for secret in git if matches(secret.url_prefix, origin)]
        on_github = github_repo(origin)
        if covering:
            # the longest prefix first; a stable sort keeps this worker's before any worker's, then the name
            survey.git[repo] = sorted(covering, key=lambda secret: -len(normalize_origin(secret.url_prefix)))[0]
        elif on_github is not None:
            survey.github[repo] = on_github
        else:
            survey.missing[repo] = (
                f"no git secret of {survey.run.owner} bound to project {survey.run.project} covers {origin}"
            )


# Asking


def _secret_lease(lease_id: int, secret: _Secret, value: str) -> Lease:
    return Lease(
        lease_id,
        secret.kind,
        "secret",
        secret.name,
        env_var=secret.env_var,
        url_prefix=secret.url_prefix,
        username=secret.username,
        value=value,
        expires_at=secret.expires_at,
    )


def _token_lease(lease_id: int, owner: str, token: str, expires_at: datetime) -> Lease:
    """The lease of a GitHub token for the repos of ``owner`` on github.com, named github-app:<owner>."""
    return Lease(
        lease_id,
        "git",
        "github-app",
        f"github-app:{owner}",
        url_prefix=f"https://{GITHUB_HOST}/{owner}",
        username=GITHUB_GIT_USERNAME,
        value=token,
        expires_at=expires_at,
    )


async def _lease_secrets(conn, survey: _Survey, worker_id: int, sealer: Sealer) -> list[Lease]:
    """A lease of each secret ``survey`` chose, the one the run holds already when there is one; a secret that does not
    open with the hub's key is left out, and the repos it was to answer for are missing."""
    rows = await (await conn.execute(LIVE_SECRET_LEASES, (survey.run.id, worker_id))).fetchall()
    held = {(secret_id, target): lease_id for secret_id, target, lease_id in rows}
    chosen: list[tuple[_Secret, str]] = [(secret, secret.env_var) for secret in survey.env]
    for secret in dict.fromkeys(survey.git.values()):
        origins = [survey.origins[repo] for repo, found in survey.git.items() if found == secret]
        chosen.append((secret, _origins_text(normalize_origin(origin) for origin in origins)))
    leases = []
    for secret, target in chosen:
        try:
            value = sealer.open(secret.sealed, secret_aad(survey.run.owner_id, secret.name, secret.kind))
        except Unsealable as exc:
            log.warning("a secret does not open; it is not leased", extra={"secret": secret.name, "why": str(exc)})
            for repo, found in list(survey.git.items()):
                if found == secret:
                    del survey.git[repo]
                    survey.missing[repo] = f"secret {secret.name} does not open with this hub's key: set it again"
            continue
        lease_id = held.get((secret.id, target))
        if lease_id is None:
            params = {
                "run": survey.run.id,
                "worker": worker_id,
                "secret": secret.id,
                "provider": "secret",
                "target": target,
                "external": None,
                "expires_at": secret.expires_at,
            }
            lease_id = (await (await conn.execute(INSERT_LEASE, params)).fetchone())[0]
        leases.append(_secret_lease(lease_id, secret, value))
    return leases


async def _lease_tokens(
    conn, survey: _Survey, worker_id: int, sealer: Sealer, made: list[InstallationToken] | None
) -> list[Lease]:
    """A lease of each token ``made``, sealed under its own id; with none made, the tokens the run holds already that
    answer for its repos on github.com, the newest first."""
    needed = {normalize_origin(survey.origins[repo]): owner for repo, (owner, _) in survey.github.items()}
    leases = []
    if made is None:
        covered: set[str] = set()
        for held in survey.tokens:
            mine = (held.origins & needed.keys()) - covered
            if not mine:
                continue
            try:
                token = sealer.open(held.sealed, lease_aad(held.id))
            except Unsealable as exc:
                log.warning("a leased GitHub token does not open", extra={"lease_id": held.id, "why": str(exc)})
                continue
            covered |= held.origins
            leases.append(_token_lease(held.id, needed[min(mine)], token, held.expires_at))
        return leases
    for token in made:
        names = {name.lower() for name in token.repositories}
        account = token.installation.account.lower()
        repos = [
            repo for repo, (owner, name) in survey.github.items() if owner.lower() == account and name.lower() in names
        ]
        if not repos:  # GitHub named the account otherwise: the repos asked for are the token's
            repos = [repo for repo, (_, name) in survey.github.items() if name.lower() in names]
        params = {
            "run": survey.run.id,
            "worker": worker_id,
            "secret": None,
            "provider": "github-app",
            "target": _origins_text(normalize_origin(survey.origins[repo]) for repo in repos),
            "external": str(token.installation.id),
            "expires_at": token.expires_at,
        }
        lease_id = (await (await conn.execute(INSERT_LEASE, params)).fetchone())[0]
        sealed = sealer.seal(token.token, lease_aad(lease_id))  # bound to the lease's id, so inserted first
        await conn.execute(SEAL_LEASE, (sealed.ciphertext, sealed.nonce, sealed.key_id, lease_id))
        owner = survey.github[repos[0]][0] if repos else token.installation.account
        leases.append(_token_lease(lease_id, owner, token.token, token.expires_at))
    return leases


def _covered(leases: list[Lease], origin: str) -> bool:
    return any(lease.kind == "git" and matches(lease.url_prefix, origin) for lease in leases)


async def _revoke_made(app: GitHubApp | None, made: list[InstallationToken] | None) -> None:
    """Revoke the tokens an ask made and could not record, for a run it then found no longer held or for a failure of
    the database; GitHub failing leaves them to expire."""
    for token in made or []:
        try:
            await app.revoke(token.token)
        except GitHubUnavailable as exc:
            log.warning("a GitHub token made for a run that ended is left to expire", extra={"why": str(exc)})


@worker_router.post("/runs/{run_id}/credentials", response_model=Credentials, responses=REFUSALS)
async def lease_credentials(request: Request, run_id: RunId, user: CurrentUser, response: Response) -> Credentials:
    """The leases of a run this worker holds: the owner's secrets for its project and a GitHub token for its repos on
    github.com, with the reason of each repo nothing covers."""
    state = request.app.state
    sealer: Sealer | None = state.sealer
    app: GitHubApp | None = state.github_app
    pool = state.pool
    async with pool.connection() as conn:
        _, survey = await _survey(conn, user, run_id, lock=False)
    made: list[InstallationToken] | None = None
    reasons: dict[str, str] = {}
    if sealer is not None and survey.github and not survey.reusable():
        if app is None:
            unset = ", ".join(state.config.github_app_missing())
            reasons = {repo: NO_APP.format(missing=unset) for repo in survey.github}
        else:
            found = await app.tokens(survey.github.values())
            made = found.tokens
            for repo, (owner, name) in survey.github.items():
                if f"{owner}/{name}" in found.missing:
                    reasons[repo] = found.missing[f"{owner}/{name}"]
    try:
        async with pool.connection() as conn:
            worker_id, survey = await _survey(conn, user, run_id, lock=True)
            if sealer is None:
                missing_key = ", ".join(state.config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
                leases: list[Lease] = []
                survey.missing = {repo: NO_KEY.format(missing=missing_key) for repo in survey.origins}
            else:
                leases = await _lease_secrets(conn, survey, worker_id, sealer)
                leases += await _lease_tokens(conn, survey, worker_id, sealer, made)
                for repo in survey.github:
                    if not _covered(leases, survey.origins[repo]):
                        survey.missing[repo] = reasons.get(repo) or (
                            f"GitHub made no token for {'/'.join(survey.github[repo])}; ask again"
                        )
            target = _lease_target(survey.run, leases)
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.CREDENTIAL_LEASE,
                target=target,
                project_id=survey.run.project_id,
            )
    except Exception:  # the tokens made are recorded nowhere: revoke them rather than leave them out for an hour
        await _revoke_made(app, made)
        raise
    missing = [
        MissingOrigin(repo=repo, origin=survey.origins[repo], reason=survey.missing[repo])
        for repo in survey.origins
        if repo in survey.missing
    ]
    extra = {
        "run_id": run_id,
        "worker_id": worker_id,
        "leases": len(leases),
        "secrets": sorted({lease.name for lease in leases if lease.provider == "secret"}),
        "github_tokens": sum(lease.provider == "github-app" for lease in leases),
        "missing": len(missing),
    }
    log.info("credentials leased", extra=extra)
    response.headers.update(NO_STORE)
    return Credentials(leases=[CredentialLease.model_validate(lease.to_json()) for lease in leases], missing=missing)


def _names(values) -> str:
    return ",".join(sorted(set(values))) or "-"


def _lease_target(run: _Run, leases: list[Lease]) -> str:
    """How a credential.lease audit row names an ask: the run, the secrets, the App's accounts and the repos."""
    secrets = _names(lease.name for lease in leases if lease.provider == "secret")
    accounts = _names(lease.name.partition(":")[2] for lease in leases if lease.provider == "github-app")
    return f"{run.target} secrets={secrets} github-app={accounts} repos={_names(run.repos)}"


# Giving back

GIVEN_BACK_RUN = "SELECT worker_id FROM runs WHERE id = %s"


@worker_router.delete("/runs/{run_id}/credentials", response_model=GivenBack, responses=REFUSALS)
async def give_back_credentials(request: Request, run_id: RunId, user: CurrentUser) -> GivenBack:
    """Give back every lease this worker holds of the run, in whatever state the run is; GitHub tokens are revoked."""
    state = request.app.state
    async with state.pool.connection() as conn:
        worker_id = (await _worker_of(conn, user))[0]
        row = await (await conn.execute(GIVEN_BACK_RUN + " FOR UPDATE", (run_id,))).fetchone()
        if row is None or row[0] != worker_id:
            raise HTTPException(404, f"this worker never held run {run_id}")
        revoked = await end_leases(
            conn, run_id=run_id, worker_id=worker_id, actor_id=user.user_id, token_id=user.token_id, by="worker"
        )
    await revoke_tokens(state.pool, state.sealer, state.github_app, run_id=run_id)
    log.info("credentials given back", extra={"run_id": run_id, "worker_id": worker_id, "revoked": revoked})
    return GivenBack(revoked=revoked)


END_LEASES = """
WITH ended AS (
    UPDATE credential_leases SET revoked_at = greatest(now(), issued_at)
     WHERE revoked_at IS NULL AND (%(run)s::bigint IS NULL OR run_id = %(run)s)
       AND (%(worker)s::bigint IS NULL OR worker_id = %(worker)s)
    RETURNING run_id, provider, secret_id
)
SELECT e.run_id, r.project_id, p.name, r.plan_id, r.step_key, count(*),
       array_remove(array_agg(DISTINCT s.name), NULL), count(*) FILTER (WHERE e.provider = 'github-app')
  FROM ended e JOIN runs r ON r.id = e.run_id JOIN projects p ON p.id = r.project_id
  LEFT JOIN secrets s ON s.id = e.secret_id
 GROUP BY e.run_id, r.project_id, p.name, r.plan_id, r.step_key
 ORDER BY e.run_id
"""


async def end_leases(
    conn,
    *,
    run_id: int | None = None,
    worker_id: int | None = None,
    actor_id: int | None = None,
    token_id: int | None = None,
    by: str,
) -> int:
    """Mark revoked, in the caller's transaction, the leases not given back yet of run ``run_id``, of worker
    ``worker_id``, or of that worker's run, and add a credential.revoke audit row per run naming ``by``; the actor is
    None for the hub itself. Their GitHub tokens keep their sealed values for ``revoke_tokens``, which revokes them at
    GitHub once the transaction commits. Returns how many leases were marked."""
    if run_id is None and worker_id is None:
        raise ValueError("end_leases needs a run, a worker or both")
    rows = await (await conn.execute(END_LEASES, {"run": run_id, "worker": worker_id})).fetchall()
    for run, project_id, project, plan_id, step_key, count, secrets, tokens in rows:
        target = (
            f"{_run_target(project, plan_id, step_key, run)} leases={count} secrets={_names(secrets)} "
            f"github-app={tokens} by={by}"
        )
        await audit.record(
            conn,
            actor_id=actor_id,
            token_id=token_id,
            action=audit.CREDENTIAL_REVOKE,
            target=target,
            project_id=project_id,
        )
    return sum(row[5] for row in rows)


PENDING_TOKENS = """
SELECT id FROM credential_leases
 WHERE sealed_value IS NOT NULL AND revoked_at IS NOT NULL AND expires_at > now()
   AND (%(run)s::bigint IS NULL OR run_id = %(run)s) AND (%(worker)s::bigint IS NULL OR worker_id = %(worker)s)
 ORDER BY id
 LIMIT %(batch)s
"""
TAKE_TOKEN = """
SELECT sealed_value, nonce, key_id FROM credential_leases
 WHERE id = %s AND sealed_value IS NOT NULL AND revoked_at IS NOT NULL
   FOR UPDATE SKIP LOCKED
"""
DROP_SEALED = "UPDATE credential_leases SET sealed_value = NULL, nonce = NULL, key_id = NULL WHERE id = %s"


async def revoke_tokens(
    pool,
    sealer: Sealer | None,
    app: GitHubApp | None,
    *,
    run_id: int | None = None,
    worker_id: int | None = None,
    batch: int = REVOKE_BATCH,
) -> dict:
    """Revoke at GitHub the tokens of leases marked revoked that still hold their sealed value and have not expired,
    of run ``run_id`` or worker ``worker_id`` when given, each in a transaction of its own that holds its row: the
    sealed value is dropped once GitHub revoked the token or no longer takes it. GitHub failing stops the pass and
    leaves the rest to the next one; a value that does not open is dropped, and its token ends at its expiry. Returns
    how many tokens were revoked, already gone, left for later and dropped unopened."""
    counts: Counter[str] = Counter()
    if sealer is None or app is None:
        return {"revoked": 0, "gone": 0, "left": 0, "unopened": 0}
    params = {"run": run_id, "worker": worker_id, "batch": batch}
    async with pool.connection() as conn:
        pending = [row[0] for row in await (await conn.execute(PENDING_TOKENS, params)).fetchall()]
    for index, lease_id in enumerate(pending):
        async with pool.connection() as conn:
            row = await (await conn.execute(TAKE_TOKEN, (lease_id,))).fetchone()
            if row is None:  # revoked meanwhile by another pass, or being revoked now
                continue
            try:
                token = sealer.open(Sealed(row[0], row[1], row[2]), lease_aad(lease_id))
            except Unsealable as exc:
                why = {"lease_id": lease_id, "why": str(exc)}
                log.warning("a leased GitHub token does not open; it ends at its expiry", extra=why)
                await conn.execute(DROP_SEALED, (lease_id,))
                counts["unopened"] += 1
                continue
            try:
                revoked = await app.revoke(token)
            except GitHubUnavailable as exc:
                counts["left"] += len(pending) - index
                log.warning("GitHub tokens left to revoke later", extra={"left": counts["left"], "why": str(exc)})
                break
            await conn.execute(DROP_SEALED, (lease_id,))
            counts["revoked" if revoked else "gone"] += 1
    report = {key: counts.get(key, 0) for key in ("revoked", "gone", "left", "unopened")}
    if pending:
        log.info("leased GitHub tokens revoked", extra={**report, "run_id": run_id, "worker_id": worker_id})
    return report


DROP_EXPIRED = """
UPDATE credential_leases SET sealed_value = NULL, nonce = NULL, key_id = NULL
 WHERE sealed_value IS NOT NULL AND expires_at <= now()
"""


async def drop_expired(conn) -> int:
    """Drop the sealed values of the GitHub tokens past their end, which GitHub no longer takes; how many."""
    return (await conn.execute(DROP_EXPIRED)).rowcount
