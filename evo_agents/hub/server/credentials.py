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
  (``GitHubApp.tokens``), for the repos the run's owner may push to on GitHub alone (``GitHubApp.push_refusal``): a
  lease named ``github-app:<account>`` of kind git for ``https://github.com/<owner>``, with GITHUB_GIT_USERNAME. The
  token of a review run or a judge run reads only (``credentials.github_permissions``): contents and metadata, read.

A run of the Curator (a review run, a judge run, or a run of a plan the Curator made: ``changes.curator_policy``) gets
less, and from elsewhere: its GitHub token comes from the Curator's App (EVO_HUB_CURATOR_APP_*), whose installation
tokens the rulesets of the repos keep off their default branches, never from the workers' App, and without the
Curator's App it gets none, every repo on GitHub missing with that reason; no git secret answers for an origin on
GitHub; for other origins only the git secret the charter names (``git_secret``) is leased, and of the env secrets only
those the charter lists (``env_secrets``). Each ask of a Builder of the Curator checks again, with the Curator's App,
that the ruleset of each of its repos on GitHub still keeps that App off the default branch
(``pulls.check_ruleset``, kept as the repo's last check): a repo whose check does not pass, or cannot be made now, gets
no token, and its reason says why.

The answer is {leases, missing}: each lease as ``evo_agents.hub.credentials.Lease.to_json`` writes it, value included,
and for each of the run's repos whose origin nothing covers, the repo, its origin and why: no origin registered, no
secret covering it, the App not installed on it or not configured, the owner not allowed to push to it on GitHub,
GitHub failing. Without EVO_HUB_SECRETS_KEY nothing is leased and every repo is missing, with that variable as the
reason. A secret is leased once per run and worker: asked again, the same lease comes back with the secret's value as
it is now. A GitHub token is kept sealed in its lease (``sealing.lease_aad``) until it expires, so the hub can still
revoke it. Asked again, the run gets the same tokens while each has GITHUB_TOKEN_REFRESH_SECONDS or more left, and new
ones, in new leases, after that; the tokens they replace go on working until they expire, so a push that took one is
not cut off. GitHub is asked between two transactions, never while the run's row is locked; the second one checks the
run is still held, and when it is not, the tokens just made are revoked and the answer is 404. Every ask adds one audit
row credential.lease naming the run, the secrets by id (``secrets=12,15``: their names are their owner's, and the audit
is read by hub admins), the App's accounts and the repos, never a value.

DELETE on the same route gives back every lease this worker holds of the run, whatever state the run is in now: the
leases are marked revoked, each GitHub token is revoked with itself (DELETE /installation/token) and its sealed value
dropped, and one audit row credential.revoke names the run and what it gave back, the secrets by id. A run of another
worker is 404.

GET /v1/projects/{p}/runs/{id}/credentials lists every lease the run got, given back or not, for the member who
dispatched it: the secret's name or ``github-app:<account>``, provider, kind, target (the variable, or the origins it
answered for), the worker, and when it was issued, ends and was revoked; never a value, sealed or not. The run must be
one the caller may read (``runs.readable_run``: 404 otherwise); another member, a hub admin included, gets 403, since
the leases name the owner's secrets as GET /v1/secrets does to the owner alone.

The owner must still hold writer on the run's project when the worker asks: once the grant is gone or lowered to
reader, the ask is 403, and the admin route that took it gave back the leases still out of every run the member
dispatched in the project (``end_member_leases``, from ``admin.grant`` and ``admin.revoke``).

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
from typing import TYPE_CHECKING, Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, distinct, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import (
    GITHUB_HOST,
    GITHUB_TOKEN_REFRESH_SECONDS,
    PROVIDERS,
    SECRET_KINDS,
    Lease,
    github_permissions,
    github_repo,
    matches,
    normalize_origin,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, InstallationToken, Pusher
from evo_agents.hub.server.runs.models import NOT_HELD, RunId
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad, secret_aad
from evo_agents.hub.server.security import CurrentUser, Principal

if TYPE_CHECKING:
    from evo_agents.hub.server.changes import CuratorPolicy

log = logging.getLogger(__name__)

GITHUB_GIT_USERNAME = "x-access-token"  # the user git sends with an installation token, as GitHub documents it
MAX_TARGET_CHARS = 2000  # credential_leases.target
REVOKE_BATCH = 500  # GitHub tokens one pass of revoke_tokens takes
REFRESH = timedelta(seconds=GITHUB_TOKEN_REFRESH_SECONDS)
NO_KEY = "this hub keeps no secrets and leases nothing: it needs {missing} (docs/credentials.md)"
NO_APP = "no git secret covers it, and this hub has no GitHub App: {missing} not set (docs/credentials.md)"
NO_CURATOR_APP = (
    "a run of the Curator gets its GitHub token from the Curator's App alone, and this hub has none: {missing} not set"
)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["runs"], responses={401: {"model": ErrorBody}})
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


class RunLease(BaseModel):
    """A lease a run got, as its owner sees it: everything but the value."""

    id: int
    name: str = Field(description="the secret's name, or github-app:<account>")
    provider: Literal[PROVIDERS] = Field(description="secret: the owner's; github-app: a token the hub's App made")
    kind: Literal[SECRET_KINDS] = Field(description="env: a variable of the agent; git: what git's helper answered")
    target: str = Field(
        description="kind env: the variable it set; kind git: the origins of the run's repos it answered for, "
        "space-separated, in the form of normalize_origin"
    )
    worker: str = Field(description="the worker it was leased to")
    issued_at: datetime
    expires_at: datetime | None = Field(description="when it stops working: a GitHub token's hour, a secret's end")
    revoked_at: datetime | None = Field(description="when the run gave it back or the hub took it; null while out")


# What a run may get


@dataclass(frozen=True)
class _Run:
    id: int
    state: str
    worker_id: int | None
    kind: str
    project_id: int
    project: str
    plan_id: str | None  # None for a review run
    step_key: str | None
    owner_id: int
    owner: str
    repos: tuple[str, ...]
    owner_github_id: int | None
    curator: CuratorPolicy | None = None  # a run of the Curator: what its charter lets it have

    @property
    def target(self) -> str:
        return run_target(self.project, self.plan_id, self.step_key, self.id)


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


def _run_row(run_id: int, *, lock: bool):
    """The run ``run_id`` with its project's name and its owner, the member who dispatched it; with ``lock`` the
    run's row is locked."""
    r, projects, users = tables.runs, tables.projects, tables.users
    statement = (
        select(
            r.c.state,
            r.c.worker_id,
            r.c.kind,
            r.c.project_id,
            projects.c.name.label("project"),
            r.c.plan_id,
            r.c.step_key,
            r.c.dispatched_by,
            users.c.login.label("owner"),
            r.c.repo,
            r.c.repos,
            users.c.github_id,
        )
        .join_from(r, projects, projects.c.id == r.c.project_id)
        .join(users, users.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )
    return statement.with_for_update(of=r) if lock else statement


def _owner_role(owner_id: int, project_id: int, *, lock: bool):
    grants = tables.grants
    statement = select(grants.c.role).where(grants.c.user_id == owner_id, grants.c.project_id == project_id)
    return statement.with_for_update(read=True) if lock else statement


def _bound_secrets(owner_id: int, project_id: int, worker_id: int, *, lock: bool):
    """The owner's secrets bound to the run's project, on any of their workers or on this one, live and not past their
    end, by name; whether each is bound to this worker. With ``lock`` their rows are shared-locked."""
    s, b = tables.secrets, tables.secret_bindings
    on_worker = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id, b.c.worker_id == worker_id)
    bound = exists().where(
        b.c.secret_id == s.c.id,
        b.c.project_id == project_id,
        or_(b.c.worker_id.is_(None), b.c.worker_id == worker_id),
    )
    statement = (
        select(
            s.c.id,
            s.c.name,
            s.c.kind,
            s.c.env_var,
            s.c.url_prefix,
            s.c.username,
            s.c.sealed,
            s.c.nonce,
            s.c.key_id,
            s.c.expires_at,
            on_worker.label("on_worker"),
        )
        .where(
            s.c.owner_id == owner_id,
            s.c.deleted_at.is_(None),
            or_(s.c.expires_at.is_(None), s.c.expires_at > func.now()),
            bound,
        )
        .order_by(s.c.name)
    )
    return statement.with_for_update(read=True, of=s) if lock else statement


def _held_tokens(run_id: int, worker_id: int):
    """The GitHub tokens the run holds on this worker, not given back, with REFRESH or more left; the newest first."""
    cl = tables.credential_leases
    return (
        select(cl.c.id, cl.c.target, cl.c.expires_at, cl.c.sealed_value, cl.c.nonce, cl.c.key_id)
        .where(
            cl.c.run_id == run_id,
            cl.c.worker_id == worker_id,
            cl.c.provider == "github-app",
            cl.c.revoked_at.is_(None),
            cl.c.sealed_value.is_not(None),
            cl.c.expires_at > func.now() + REFRESH,
        )
        .order_by(cl.c.id.desc())
    )


async def _insert_lease(conn: AsyncConnection, **values) -> int:
    cl = tables.credential_leases
    return (await conn.execute(insert(cl).values(**values).returning(cl.c.id))).scalar_one()


def _run_repos(kind: str, repo: str | None, repos) -> tuple[str, ...]:
    if kind != "step":  # a plan run's repos, or a review run's
        names = [entry.get("repo") for entry in repos or [] if isinstance(entry, dict)]
    else:
        names = [repo]
    return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name))


async def _held_run(conn: AsyncConnection, worker_id: int, run_id: int, *, lock: bool) -> _Run:
    """The run ``run_id`` that worker ``worker_id`` holds, its row locked with ``lock``; 404 for any other. 403 when
    the member who dispatched it no longer holds writer on its project; with ``lock`` their grant's row is held too,
    so a grant taken away meanwhile waits for the leases of this ask, and takes them back (``end_member_leases``)."""
    row = (await conn.execute(_run_row(run_id, lock=lock))).one_or_none()
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise HTTPException(404, NOT_HELD.format(id=run_id))
    role = (await conn.execute(_owner_role(row.dispatched_by, row.project_id, lock=lock))).scalar_one_or_none()
    if not has_role(role, "writer"):
        raise HTTPException(
            403,
            f"{row.owner}, who dispatched run {run_id}, no longer holds the writer role on project {row.project}: "
            "the run gets no credentials",
        )
    from evo_agents.hub.server.changes import curator_policy  # it reads runs through the routes that import this one

    return _Run(
        curator=await curator_policy(conn, row.project_id, row.kind, row.plan_id),
        id=run_id,
        state=row.state,
        worker_id=row.worker_id,
        kind=row.kind,
        project_id=row.project_id,
        project=row.project,
        plan_id=row.plan_id,
        step_key=row.step_key,
        owner_id=row.dispatched_by,
        owner=row.owner,
        repos=_run_repos(row.kind, row.repo, row.repos),
        owner_github_id=row.github_id,
    )


def _origins_text(origins) -> str:
    """The origins a lease answers for, as credential_leases.target keeps them: space-separated, within its limit."""
    kept: list[str] = []
    for origin in sorted(set(origins)):
        if len(" ".join([*kept, origin])) > MAX_TARGET_CHARS:
            break
        kept.append(origin)
    return " ".join(kept) or "-"


async def _survey(conn: AsyncConnection, user: Principal, run_id: int, *, lock: bool) -> tuple[int, _Survey]:
    """(worker id, what run ``run_id`` may get) for the worker of ``user``, which must hold the run. The worker's row
    is locked, as on every worker route; with ``lock`` the run's and the secrets' rows are too, so the run cannot end
    and a secret cannot be deleted until the leases are recorded."""
    worker_id = (await worker_of(conn, user))[0]
    run = await _held_run(conn, worker_id, run_id, lock=lock)
    repos = tables.project_repos
    origins = select(repos.c.name, repos.c.origin).where(
        repos.c.project_id == run.project_id, repos.c.name.in_(list(run.repos))
    )
    registered = {row.name: row.origin for row in await conn.execute(origins)}
    rows = await conn.execute(_bound_secrets(run.owner_id, run.project_id, worker_id, lock=lock))
    allowed = _allowed(run.curator)
    secrets = [
        _Secret(
            id=row.id,
            name=row.name,
            kind=row.kind,
            env_var=row.env_var,
            url_prefix=row.url_prefix,
            username=row.username,
            sealed=Sealed(row.sealed, row.nonce, row.key_id),
            expires_at=row.expires_at,
            on_worker=row.on_worker,
        )
        for row in rows
        if allowed is None or (row.kind, row.name) in allowed
    ]
    tokens = [
        _HeldToken(
            id=row.id,
            origins=frozenset(row.target.split()),
            expires_at=row.expires_at,
            sealed=Sealed(row.sealed_value, row.nonce, row.key_id),
        )
        for row in await conn.execute(_held_tokens(run_id, worker_id))
    ]
    survey = _Survey(run, {repo: registered.get(repo) for repo in run.repos}, secrets, tokens)
    _choose(survey)
    return worker_id, survey


def _allowed(policy: CuratorPolicy | None) -> set[tuple[str, str]] | None:
    """(kind, name) of the secrets a run of the Curator may be leased: the charter's git secret and env secrets; None
    for any other run, which may be leased all its owner bound."""
    if policy is None:
        return None
    allowed = {("env", name) for name in policy.env_secrets}
    if policy.git_secret:
        allowed.add(("git", policy.git_secret))
    return allowed


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
        on_github = github_repo(origin)
        if survey.run.curator is not None and on_github is not None:  # the Curator's App, never a member's secret
            survey.github[repo] = on_github
            continue
        covering = [secret for secret in git if matches(secret.url_prefix, origin)]
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


async def _lease_secrets(conn: AsyncConnection, survey: _Survey, worker_id: int, sealer: Sealer) -> list[Lease]:
    """A lease of each secret ``survey`` chose, the one the run holds already when there is one; a secret that does not
    open with the hub's key is left out, and the repos it was to answer for are missing."""
    cl = tables.credential_leases
    live = select(cl.c.secret_id, cl.c.target, cl.c.id).where(
        cl.c.run_id == survey.run.id,
        cl.c.worker_id == worker_id,
        cl.c.provider == "secret",
        cl.c.revoked_at.is_(None),
    )
    held = {(row.secret_id, row.target): row.id for row in await conn.execute(live)}
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
            lease_id = await _insert_lease(
                conn,
                run_id=survey.run.id,
                worker_id=worker_id,
                secret_id=secret.id,
                provider="secret",
                target=target,
                external_id=None,
                expires_at=secret.expires_at,
            )
        leases.append(_secret_lease(lease_id, secret, value))
    return leases


async def _lease_tokens(
    conn: AsyncConnection, survey: _Survey, worker_id: int, sealer: Sealer, made: list[InstallationToken] | None
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
        lease_id = await _insert_lease(
            conn,
            run_id=survey.run.id,
            worker_id=worker_id,
            secret_id=None,
            provider="github-app",
            target=_origins_text(normalize_origin(survey.origins[repo]) for repo in repos),
            external_id=str(token.installation.id),
            expires_at=token.expires_at,
        )
        sealed = sealer.seal(token.token, lease_aad(lease_id))  # bound to the lease's id, so inserted first
        cl = tables.credential_leases
        await conn.execute(
            update(cl)
            .values(sealed_value=sealed.ciphertext, nonce=sealed.nonce, key_id=sealed.key_id)
            .where(cl.c.id == lease_id)
        )
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
    engine = state.engine
    async with engine.begin() as conn:
        _, survey = await _survey(conn, user, run_id, lock=False)
    curator = survey.run.curator is not None
    app: GitHubApp | None = getattr(state, "curator_app", None) if curator else state.github_app
    made: list[InstallationToken] | None = None
    reasons: dict[str, str] = {}
    unchecked: dict[str, str] = {}
    if sealer is not None and app is not None and curator and survey.run.curator.role == "builder" and survey.github:
        unchecked = await _rulesets_now(engine, app, survey)
        _drop_github(survey, unchecked)
    if sealer is not None and survey.github and not survey.reusable():
        if app is None:
            unset = ", ".join(state.config.curator_app_missing() if curator else state.config.github_app_missing())
            refusal = NO_CURATOR_APP if curator else NO_APP
            reasons = {repo: refusal.format(missing=unset) for repo in survey.github}
        else:
            pusher = Pusher(survey.run.owner, survey.run.owner_github_id)
            found = await app.tokens(survey.github.values(), pusher, github_permissions(survey.run.kind))
            made = found.tokens
            for repo, (owner, name) in survey.github.items():
                if f"{owner}/{name}" in found.missing:
                    reasons[repo] = found.missing[f"{owner}/{name}"]
    try:
        async with engine.begin() as conn:
            worker_id, survey = await _survey(conn, user, run_id, lock=True)
            _drop_github(survey, unchecked)
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
            target = _lease_target(survey.run, leases, {secret.name: secret.id for secret in survey.secrets})
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


async def _rulesets_now(engine: AsyncEngine, app: GitHubApp, survey: _Survey) -> dict[str, str]:
    """For a Builder of the Curator: check again, with the Curator's App, the ruleset of each of its repos on GitHub,
    and keep what each check found; repo -> why a repo whose ruleset does not keep the Curator off, or could not be
    checked now, gets no token."""
    from evo_agents.hub.server.changes import _store_check  # it reads runs through the routes that import this one
    from evo_agents.hub.server.pulls import check_ruleset

    refused: dict[str, str] = {}
    for repo, (owner, name) in survey.github.items():
        try:
            found = await check_ruleset(app, owner, name)
        except GitHubUnavailable as exc:
            refused[repo] = (
                f"the hub could not check the ruleset of {owner}/{name} now ({exc}): a Builder gets no token"
            )
            continue
        async with engine.begin() as conn:
            await _store_check(conn, survey.run.project_id, repo, f"{owner}/{name}", found, None)
        if not found.protected:
            refused[repo] = (
                f"the ruleset of {owner}/{name} no longer keeps the Curator's App off its default branch "
                f"({found.reason}): a Builder gets no token for it"
            )
    if refused:
        log.warning(
            "a builder's repos failed their ruleset check", extra={"run_id": survey.run.id, "repos": sorted(refused)}
        )
    return refused


def _drop_github(survey: _Survey, refused: dict[str, str]) -> None:
    """Take the repos of ``refused`` out of those that get a GitHub token, each missing with its reason."""
    for repo, why in refused.items():
        survey.github.pop(repo, None)
        survey.missing[repo] = why


def _names(values) -> str:
    return ",".join(sorted(set(values))) or "-"


def _ids(values) -> str:
    return ",".join(str(value) for value in sorted(set(values))) or "-"


def _lease_target(run: _Run, leases: list[Lease], secret_ids: dict[str, int]) -> str:
    """How a credential.lease audit row names an ask: the run, the secrets by id (their names are their owner's, and
    the audit is read by hub admins), the App's accounts and the repos."""
    secrets = _ids(secret_ids[lease.name] for lease in leases if lease.provider == "secret")
    accounts = _names(lease.name.partition(":")[2] for lease in leases if lease.provider == "github-app")
    return f"{run.target} secrets={secrets} github-app={accounts} repos={_names(run.repos)}"


# Giving back


@worker_router.delete("/runs/{run_id}/credentials", response_model=GivenBack, responses=REFUSALS)
async def give_back_credentials(request: Request, run_id: RunId, user: CurrentUser) -> GivenBack:
    """Give back every lease this worker holds of the run, in whatever state the run is; GitHub tokens are revoked."""
    state = request.app.state
    async with state.engine.begin() as conn:
        worker_id = (await worker_of(conn, user))[0]
        r = tables.runs
        row = (await conn.execute(select(r.c.worker_id).where(r.c.id == run_id).with_for_update())).one_or_none()
        if row is None or row.worker_id != worker_id:
            raise HTTPException(404, f"this worker never held run {run_id}")
        revoked = await end_leases(
            conn, run_id=run_id, worker_id=worker_id, actor_id=user.user_id, token_id=user.token_id, by="worker"
        )
    await revoke_tokens(state.engine, state.sealer, revoker(state), run_id=run_id)
    log.info("credentials given back", extra={"run_id": run_id, "worker_id": worker_id, "revoked": revoked})
    return GivenBack(revoked=revoked)


# The owner's view


def _run_owner(run_id: int):
    r, users = tables.runs, tables.users
    return (
        select(r.c.dispatched_by, users.c.login)
        .join_from(r, users, users.c.id == r.c.dispatched_by)
        .where(r.c.id == run_id)
    )


def _run_leases(run_id: int):
    """Every lease of run ``run_id``, with the worker's name and the secret's name and kind (none for a GitHub token),
    in the order they were made."""
    cl, w, s = tables.credential_leases, tables.workers, tables.secrets
    return (
        select(
            cl.c.id,
            s.c.name.label("secret"),
            cl.c.provider,
            s.c.kind.label("secret_kind"),
            cl.c.target,
            w.c.name.label("worker"),
            cl.c.issued_at,
            cl.c.expires_at,
            cl.c.revoked_at,
        )
        .join_from(cl, w, w.c.id == cl.c.worker_id)
        .outerjoin(s, s.c.id == cl.c.secret_id)
        .where(cl.c.run_id == run_id)
        .order_by(cl.c.id)
    )


def _token_name(target: str) -> str:
    """The name a GitHub token's lease was handed out with, github-app:<owner>, from the origins it answered for."""
    origins = target.split()
    found = github_repo(origins[0]) if origins else None
    return f"github-app:{found[0]}" if found else "github-app"


@router.get("/{project}/runs/{run_id}/credentials", response_model=list[RunLease], responses=REFUSALS)
async def run_credentials(
    request: Request,
    project: ProjectName,
    run_id: RunId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> list[RunLease]:
    """Every lease the run got, given back or not, without a value; for the member who dispatched it."""
    async with request.app.state.engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        owner_id, owner = (await conn.execute(_run_owner(run_id))).one()
        if owner_id != user.user_id:
            raise HTTPException(403, f"only {owner}, who dispatched run {run_id}, sees the credentials it got")
        rows = (await conn.execute(_run_leases(run_id))).all()
    return [
        RunLease(
            id=row.id,
            name=row.secret if row.provider == "secret" else _token_name(row.target),
            provider=row.provider,
            kind=row.secret_kind if row.provider == "secret" else "git",
            target=row.target,
            worker=row.worker,
            issued_at=row.issued_at,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


def _end_leases(run_id: int | None, worker_id: int | None):
    """One statement: mark revoked the leases not given back yet of run ``run_id`` and of worker ``worker_id`` (either
    may be None), in a data-modifying CTE, and per run they were of, its project, plan and step, how many leases ended,
    the secrets among them and how many GitHub tokens."""
    cl, r, projects = tables.credential_leases, tables.runs, tables.projects
    ending = update(cl).values(revoked_at=func.greatest(func.now(), cl.c.issued_at)).where(cl.c.revoked_at.is_(None))
    if run_id is not None:
        ending = ending.where(cl.c.run_id == run_id)
    if worker_id is not None:
        ending = ending.where(cl.c.worker_id == worker_id)
    ended = ending.returning(cl.c.run_id, cl.c.provider, cl.c.secret_id).cte("ended")
    secrets = func.array_remove(func.array_agg(distinct(ended.c.secret_id)), None, type_=ARRAY(BigInteger))
    return (
        select(
            ended.c.run_id,
            r.c.project_id,
            projects.c.name.label("project"),
            r.c.plan_id,
            r.c.step_key,
            func.count().label("leases"),
            secrets.label("secrets"),
            func.count().filter(ended.c.provider == "github-app").label("tokens"),
        )
        .join_from(ended, r, r.c.id == ended.c.run_id)
        .join(projects, projects.c.id == r.c.project_id)
        .group_by(ended.c.run_id, r.c.project_id, projects.c.name, r.c.plan_id, r.c.step_key)
        .order_by(ended.c.run_id)
    )


async def end_leases(
    conn: AsyncConnection,
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
    rows = (await conn.execute(_end_leases(run_id, worker_id))).all()
    for row in rows:
        target = (
            f"{run_target(row.project, row.plan_id, row.step_key, row.run_id)} leases={row.leases} "
            f"secrets={_ids(row.secrets)} github-app={row.tokens} by={by}"
        )
        await audit.record(
            conn,
            actor_id=actor_id,
            token_id=token_id,
            action=audit.CREDENTIAL_REVOKE,
            target=target,
            project_id=row.project_id,
        )
    return sum(row.leases for row in rows)


async def end_member_leases(
    conn: AsyncConnection, *, user_id: int, project_id: int, actor_id: int | None, token_id: int | None, by: str
) -> list[int]:
    """Mark revoked, in the caller's transaction, every lease still out of the runs member ``user_id`` dispatched in
    project ``project_id``, as ``end_leases`` does for each run: the member no longer holds writer there. Returns the
    runs, whose GitHub tokens the caller revokes once the transaction commits (``revoke_tokens``)."""
    cl, r = tables.credential_leases, tables.runs
    leased = (
        select(cl.c.run_id)
        .distinct()
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.revoked_at.is_(None), r.c.project_id == project_id, r.c.dispatched_by == user_id)
        .order_by(cl.c.run_id)
    )
    run_ids = list((await conn.execute(leased)).scalars())
    for run_id in run_ids:
        await end_leases(conn, run_id=run_id, actor_id=actor_id, token_id=token_id, by=by)
    return run_ids


def _pending_tokens(run_id: int | None, worker_id: int | None, batch: int):
    """The first ``batch`` leases marked revoked that still hold a sealed GitHub token that has not expired, of run
    ``run_id`` and of worker ``worker_id`` when given."""
    cl = tables.credential_leases
    statement = select(cl.c.id).where(
        cl.c.sealed_value.is_not(None), cl.c.revoked_at.is_not(None), cl.c.expires_at > func.now()
    )
    if run_id is not None:
        statement = statement.where(cl.c.run_id == run_id)
    if worker_id is not None:
        statement = statement.where(cl.c.worker_id == worker_id)
    return statement.order_by(cl.c.id).limit(batch)


def _take_token(lease_id: int):
    """The sealed token of lease ``lease_id``, its row locked, unless another pass holds it or dropped it."""
    cl = tables.credential_leases
    return (
        select(cl.c.sealed_value, cl.c.nonce, cl.c.key_id)
        .where(cl.c.id == lease_id, cl.c.sealed_value.is_not(None), cl.c.revoked_at.is_not(None))
        .with_for_update(skip_locked=True)
    )


def _drop_sealed(*where):
    cl = tables.credential_leases
    return update(cl).values(sealed_value=None, nonce=None, key_id=None).where(*where)


def revoker(state) -> GitHubApp | None:
    """The client that revokes the leased GitHub tokens: either App's, since a token is revoked with itself."""
    return state.github_app or getattr(state, "curator_app", None)


async def revoke_tokens(
    engine: AsyncEngine,
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
    cl = tables.credential_leases
    async with engine.begin() as conn:
        pending = list((await conn.execute(_pending_tokens(run_id, worker_id, batch))).scalars())
    for index, lease_id in enumerate(pending):
        async with engine.begin() as conn:
            row = (await conn.execute(_take_token(lease_id))).one_or_none()
            if row is None:  # revoked meanwhile by another pass, or being revoked now
                continue
            try:
                token = sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(lease_id))
            except Unsealable as exc:
                why = {"lease_id": lease_id, "why": str(exc)}
                log.warning("a leased GitHub token does not open; it ends at its expiry", extra=why)
                await conn.execute(_drop_sealed(cl.c.id == lease_id))
                counts["unopened"] += 1
                continue
            try:
                revoked = await app.revoke(token)
            except GitHubUnavailable as exc:
                counts["left"] += len(pending) - index
                log.warning("GitHub tokens left to revoke later", extra={"left": counts["left"], "why": str(exc)})
                break
            await conn.execute(_drop_sealed(cl.c.id == lease_id))
            counts["revoked" if revoked else "gone"] += 1
    report = {key: counts.get(key, 0) for key in ("revoked", "gone", "left", "unopened")}
    if pending:
        log.info("leased GitHub tokens revoked", extra={**report, "run_id": run_id, "worker_id": worker_id})
    return report


async def drop_expired(conn: AsyncConnection) -> int:
    """Drop the sealed values of the GitHub tokens past their end, which GitHub no longer takes; how many."""
    cl = tables.credential_leases
    return (await conn.execute(_drop_sealed(cl.c.sealed_value.is_not(None), cl.c.expires_at <= func.now()))).rowcount
