"""The leases of a run: what the worker holding a run gets of its owner's secrets and of the hub's GitHub App, and how
the run gives them back (``docs/credentials.md``).

POST /v1/worker/runs/{id}/credentials, with the worker's token and the protocol header as on every worker route, asks
for the leases of a run the worker holds, in one of ``runs.HELD_STATES`` (``waiting`` of a plan run among them); any
other run, another worker's or one that ended, is 404 as for a state report. The hub takes the run's repos (its repo
for a run of one step, its repos for a plan run, and for a chat run those it started with and those its agent opened
since, ``chat_agents.open_repo``) and their origins in project_repos (the run's project's, else another project's the
owner holds a grant on: ``repo_origins``), compared in the form of ``credentials.normalize_origin``, and leases what the run's owner, the member who dispatched it, bound to the run's
project, on any of their workers or on this one, and that has not expired, never one of kind forge (``LEASED_KINDS``):

- each secret of kind env, one per variable: the account the run took already, else one bound to this worker before
  one bound to any, by the strategy of the owner's policy for the project (``account_choice``);
- each secret of kind git whose url_prefix covers one of the origins, the longest prefix for each origin, so a
  secret for ``https://gitlab.example.org/group`` answers for ``git@gitlab.example.org:group/repo.git`` too;
- a token of the hub's GitHub App for the origins on github.com that no git secret covers, one per installation
  (``GitHubApp.tokens``), for the repos the run's owner may push to on GitHub alone (``GitHubApp.push_refusal``): a
  lease named ``github-app:<account>`` of kind git for ``https://github.com/<owner>``, with GITHUB_GIT_USERNAME. The
  token of a review run or a judge run reads only (``credentials.github_permissions``): contents and metadata, read.

A run of the Curator (a review run, a judge run, or a run of a plan the Curator made: ``changes.curator_policy``) gets
less, and from elsewhere: its GitHub token comes from the Curator's App (EVO_HUB_CURATOR_APP_*), whose installation
tokens the rulesets of the repos keep off their default branches, never from the workers' App, and without the Curator's
App it gets none, every repo on GitHub missing with that reason; no git secret answers for an origin on GitHub; for
other origins only the git secret the charter names (``git_secret``) is leased, and of the env secrets only those the
charter lists (``env_secrets``). Each ask of a Builder of the Curator checks again, with the Curator's App, that the
ruleset of each of its repos on GitHub still keeps that App off the default branch (``pulls.check_ruleset``, kept as the
repo's last check): a repo whose check does not pass, or cannot be made now, gets no token, and its reason says why.

A release run gets its runtime's login alone of its owner's secrets (``credentials.RELEASE_ENV_VARS``), no git secret,
and for its repo on GitHub a token of the workers' App without workflows (``credentials.github_workflows``), which its
worker hands the daemon's git alone (``evo_agents.worker.credentials.RunCredentials.guarded``).

A run's agent profile (``evo_agents.hub.profiles``), at the version the run went under, narrows that further: of the
secrets above, only those its credential allowlist names are leased, and a token of the hub's GitHub App only when it
names ``github-app`` (a repo it would cover is missing, with the profile as the reason). Each lease records the profile
and version it was handed out under; the credential.lease audit row names them (``profile=@<handle>:v<n>``) unless
the profile is the project's default.

The accounts of a run's agent (``agent_accounts``; a run under a named profile that is not the project's default,
never a run of the Curator) win over the dispatcher's, variable by variable: for each variable the agent has a live
account bound to this worker for (past its end or not), the run is leased one of those, chosen as ``account_choice``
says by DEFAULT_POLICY, weight and priority, and never a secret of the dispatcher for that variable, whatever the
profile's credential axis says about it; when every one of them is paused, cooling or past its end, the run is not
leased the dispatcher's instead (``account_choice`` says why in an event). Every other variable is the dispatcher's as
above, and on a worker the agent has no account on nothing changes. An account's lease is named ``@<handle>:<name>``
(``credentials.agent_lease_name``), the audit row names it by id and handle (`` agent-accounts=12@<handle>``), and it
is opened with the agent's own associated data (``sealing.agent_secret_aad``).

The answer is {leases, missing, repos}: each lease as ``evo_agents.hub.credentials.Lease.to_json`` writes it, value
included; each repo of the run with its origin, its path and the project that registered them, which a worker without a
checkout of it clones from; and for each of the run's repos whose origin nothing covers, the repo, its origin and why:
no origin registered, no
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

GET /v1/projects/{p}/runs/{id}/credentials, the owner's view of a run's leases, is in ``run_leases``.

The owner must still hold writer on the run's project when the worker asks: once the grant is gone or lowered to
reader, the ask is 403, and the admin route that took it gave back the leases still out of every run the member
dispatched in the project (``end_member_leases``, from ``admin.grant`` and ``admin.revoke``). So once the owner's
primary seat in the org chart ended, on every project (``org_expiry.take_back``).

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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, releases, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import (
    GITHUB_HOST,
    GITHUB_TOKEN_REFRESH_SECONDS,
    RELEASE_ENV_VARS,
    Lease,
    agent_lease_name,
    github_permissions,
    github_repo,
    github_workflows,
    matches,
    normalize_origin,
)
from evo_agents.hub.server import audit, lease_autonomy
from evo_agents.hub.server.account_choice import choose_accounts, fail_over_relogin
from evo_agents.hub.server.account_refresh import refresh_due
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.credential_models import (
    CredentialLease,
    Credentials,
    GivenBack,
    MissingOrigin,
    RunRepoSource,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.github import GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, InstallationToken, Pusher
from evo_agents.hub.server.lease_ending import (  # noqa: F401  (re-exported: callers read credentials.<name>)
    drop_expired,
    end_leases,
    end_member_leases,
    revoke_tokens,
    revoker,
)
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.repo_origins import RepoOrigin, repo_origins
from evo_agents.hub.server.runs.models import NOT_HELD, RunId
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, agent_secret_aad, lease_aad, secret_aad
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.share_leases import Refusal, end_unlent, note_refusals, own_secrets, shared_secrets

if TYPE_CHECKING:
    from evo_agents.hub.autonomy_levels import Choice
    from evo_agents.hub.server.changes import CuratorPolicy

log = logging.getLogger(__name__)

GITHUB_GIT_USERNAME = "x-access-token"  # the user git sends with an installation token, as GitHub documents it
MAX_TARGET_CHARS = 2000  # credential_leases.target
REFRESH = timedelta(seconds=GITHUB_TOKEN_REFRESH_SECONDS)
NO_KEY = "this hub keeps no secrets and leases nothing: it needs {missing} (docs/credentials.md)"
NO_APP = "no git secret covers it, and this hub has no GitHub App: {missing} not set (docs/credentials.md)"
NO_CURATOR_APP = (
    "a run of the Curator gets its GitHub token from the Curator's App alone, and this hub has none: {missing} not set"
)
RELEASE_OFF_GITHUB = "a release run gets no git secret: it pushes to GitHub alone, with the hub's App, not {origin}"

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


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
    profile_id: int | None = None  # the agent profile it went under, at profile_version; None before schema 0030
    profile_version: int | None = None
    profile: str | None = None  # its handle
    profile_default: bool = False  # the project's default profile, which the audit leaves unnamed
    credentials: dict | None = None  # its credential axis at that version
    profile_project_id: int | None = None  # the project of the profile: where its accounts are bound
    principal_id: int | None = None  # who it acts for (runs.principal_id); None for a local run or one before 0113

    @property
    def agent_owned(self) -> bool:
        """Whether the run goes under an agent with a name, whose accounts it is leased (see the module): not a run of
        the Curator, not the project's default profile, which has none."""
        return self.profile_id is not None and not self.profile_default and self.curator is None

    @property
    def target(self) -> str:
        return run_target(self.project, self.plan_id, self.step_key, self.id)

    @property
    def release(self) -> bool:
        """A release run: leased its runtime's login and a token of the workers' App alone (see the module)."""
        return self.kind == releases.RELEASE_KIND

    def allows(self, name: str) -> bool:
        """Whether the run's profile lets it be leased the credential ``name`` (a secret's, or github-app)."""
        return profiles.allows({"credential": self.credentials} if self.credentials else None, "credential", name)

    def lease_columns(self) -> dict:
        """What each lease of the run records of it: its profile and the member it is made for."""
        return {"profile_id": self.profile_id, "profile_version": self.profile_version, "principal_id": self.principal}

    @property
    def principal(self) -> int:
        """The member the run acts for (``run_autonomy``): its principal, else the member who dispatched it."""
        return self.owner_id if self.principal_id is None else self.principal_id

    def profile_text(self) -> str:
        if self.profile is None or self.profile_default:
            return ""
        return f" profile=@{self.profile}:v{self.profile_version}"


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
    owner_id: int | None  # the member who owns it; None for an agent's account
    share_id: int | None = None  # the share another member lent it under (``share_leases``); None for the run's owner's
    agent: str | None = None  # the handle of the agent profile that owns it (``agent_accounts``); None: a member's
    profile_id: int | None = None  # that profile

    @property
    def lease_name(self) -> str:
        """The name its lease carries: the secret's, or ``@<handle>:<name>`` for an agent's account, which no secret of a
        member can have, so no two leases of an answer share a name."""
        return self.name if self.agent is None else agent_lease_name(self.agent, self.name)


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
    places: dict[str, RepoOrigin] = field(default_factory=dict)  # repo -> where its origin and path come from
    env: list[_Secret] = field(default_factory=list)
    ended: tuple[str, ...] = ()  # variables whose accounts of the agent all ended: leased from no one
    git: dict[str, _Secret] = field(default_factory=dict)  # repo -> the secret that answers for its origin
    github: dict[str, tuple[str, str]] = field(default_factory=dict)  # repo -> (owner, repo) on github.com
    missing: dict[str, str] = field(default_factory=dict)  # repo -> why nothing covers its origin
    k7: Choice | None = None  # K7 of the run's principal now (lease_autonomy); None for a run without a snapshot
    read_only: dict[str, str] = field(default_factory=dict)  # repo on github.com -> why K7 gives it a token that reads
    withheld: list[str] = field(default_factory=list)  # what K7 kept from the run, for the audit row
    refused: list[Refusal] = field(default_factory=list)  # the shares that lent the run nothing, and why
    leased: dict[int, _Secret] = field(default_factory=dict)  # lease id -> the secret leased under it

    def reusable(self) -> bool:
        """Whether the GitHub tokens the run holds cover every repo that needs one; never under a K7 that reads."""
        if self.read_only:
            return False
        held = set().union(*(token.origins for token in self.tokens)) if self.tokens else set()
        return all(normalize_origin(self.origins[repo]) in held for repo in self.github)


def _run_row(run_id: int, *, lock: bool):
    """The run ``run_id`` with its project's name and its owner, the member who dispatched it; with ``lock`` the
    run's row is locked."""
    r, projects, users, ap, v = (
        tables.runs,
        tables.projects,
        tables.users,
        tables.agent_profiles,
        tables.agent_profile_versions,
    )
    credentials = (
        select(v.c.body["permissions"]["credential"])
        .where(v.c.profile_id == r.c.profile_id, v.c.version == r.c.profile_version)
        .scalar_subquery()
    )
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
            r.c.principal_id,
            users.c.login.label("owner"),
            r.c.repo,
            r.c.repos,
            users.c.github_id,
            r.c.profile_id,
            r.c.profile_version,
            ap.c.handle.label("profile"),
            func.coalesce(ap.c.is_default, False).label("profile_default"),
            credentials.label("credentials"),
            ap.c.project_id.label("profile_project_id"),
        )
        .join_from(r, projects, projects.c.id == r.c.project_id)
        .join(users, users.c.id == r.c.dispatched_by)
        .outerjoin(ap, ap.c.id == r.c.profile_id)
        .where(r.c.id == run_id)
    )
    return statement.with_for_update(of=r) if lock else statement


def _owner_role(owner_id: int, project_id: int, *, lock: bool):
    grants = tables.grants
    statement = select(grants.c.role).where(grants.c.user_id == owner_id, grants.c.project_id == project_id)
    return statement.with_for_update(read=True) if lock else statement


def _agent_accounts(run: _Run, worker_id: int, *, lock: bool):
    """The accounts of the run's agent bound to this worker, live (not deleted) and past their end or not, by name. With
    ``lock`` their rows are shared-locked."""
    s, b = tables.secrets, tables.secret_bindings
    bound = exists().where(
        b.c.secret_id == s.c.id, b.c.project_id == run.profile_project_id, b.c.worker_id == worker_id
    )
    statement = (
        select(
            s.c.id,
            s.c.name,
            s.c.kind,
            s.c.env_var,
            s.c.sealed,
            s.c.nonce,
            s.c.key_id,
            s.c.expires_at,
            s.c.environment,
            func.coalesce(s.c.expires_at <= func.now(), False).label("ended"),
        )
        .where(s.c.profile_id == run.profile_id, s.c.kind == "env", s.c.deleted_at.is_(None), bound)
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
    ended = await ended_refusal(conn, row.dispatched_by, row.owner, what=f"leasing run {run_id}", lock=lock)
    if ended is not None:
        raise HTTPException(403, ended)
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
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        profile=row.profile,
        profile_default=row.profile_default,
        credentials=row.credentials,
        profile_project_id=row.profile_project_id,
        principal_id=row.principal_id,
    )


def _origins_text(origins) -> str:
    """The origins a lease answers for, as credential_leases.target keeps them: space-separated, within its limit."""
    kept: list[str] = []
    for origin in sorted(set(origins)):
        if len(" ".join([*kept, origin])) > MAX_TARGET_CHARS:
            break
        kept.append(origin)
    return " ".join(kept) or "-"


async def survey_run(
    conn: AsyncConnection, user: Principal, run_id: int, *, lock: bool, refreshable: bool = False
) -> tuple[int, _Survey]:
    """(worker id, what run ``run_id`` may get) for the worker of ``user``, which must hold the run. The worker's row
    is locked, as on every worker route; with ``lock`` the run's and the secrets' rows are too, so the run cannot end
    and a secret cannot be deleted until the leases are recorded."""
    worker_id = (await worker_of(conn, user))[0]
    run = await _held_run(conn, worker_id, run_id, lock=lock)
    places = await repo_origins(conn, run.project_id, run.owner_id, run.repos)
    rows = (
        await conn.execute(own_secrets(run.owner_id, run.project_id, worker_id, lock=lock, refreshable=refreshable))
    ).all()
    refused: list[Refusal] = []
    if run.curator is None and not run.release:  # their charter and their runtime's login are their own
        shared, refused = await shared_secrets(conn, run, worker_id, lock=lock, refreshable=refreshable)
        rows += shared
    allowed = _allowed(run.curator)
    rows = [row for row in rows if _leasable(run, allowed, row)]
    lending = [row.share_id for row in rows if getattr(row, "share_id", None) is not None]
    k7 = await lease_autonomy.k7(conn, run.id, lending)  # a share's level holds the run from the lease that lends it
    accounts = []
    if run.agent_owned:  # its own accounts for the variables it has any for on this worker (the module)
        accounts = [
            row
            for row in await conn.execute(_agent_accounts(run, worker_id, lock=lock))
            if not run.release or row.env_var in RELEASE_ENV_VARS
        ]
    withheld = [row for row in [*rows, *accounts] if not lease_autonomy.secret_allowed(k7, *row_k7(row))]
    rows = [row for row in rows if lease_autonomy.secret_allowed(k7, *row_k7(row))]
    accounts = [row for row in accounts if lease_autonomy.secret_allowed(k7, *row_k7(row))]
    claimed = {row.env_var for row in accounts}
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
            owner_id=row.owner_id,
            share_id=getattr(row, "share_id", None),
        )
        for row in rows
        if not (row.kind == "env" and row.env_var in claimed)
    ]
    secrets += [
        _Secret(
            id=row.id,
            name=row.name,
            kind=row.kind,
            env_var=row.env_var,
            url_prefix=None,
            username=None,
            sealed=Sealed(row.sealed, row.nonce, row.key_id),
            expires_at=row.expires_at,
            on_worker=True,
            owner_id=None,
            agent=run.profile,
            profile_id=run.profile_id,
        )
        for row in accounts
        if not row.ended
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
    origins = {repo: places[repo].origin if repo in places else None for repo in run.repos}
    survey = _Survey(run, origins, secrets, tokens, places, k7=k7, refused=refused)
    survey.withheld = [f"env:{row.name}" for row in withheld]
    survey.ended = tuple(sorted(claimed - {row.env_var for row in accounts if not row.ended}))
    _choose(survey)
    return worker_id, survey


def row_k7(row) -> tuple[str, str | None, str | None]:
    """(kind, env var, environment) of a secret's row, what K7 reads of it (``lease_autonomy.secret_allowed``)."""
    return row.kind, row.env_var, row.environment


def _allowed(policy: CuratorPolicy | None) -> set[tuple[str, str]] | None:
    """(kind, name) of the secrets a run of the Curator may be leased: the charter's git secret and env secrets; None
    for any other run, which may be leased all its owner bound."""
    if policy is None:
        return None
    allowed = {("env", name) for name in policy.env_secrets}
    if policy.git_secret:
        allowed.add(("git", policy.git_secret))
    return allowed


def _leasable(run: _Run, allowed: set[tuple[str, str]] | None, row) -> bool:
    """Whether ``run`` may be leased the secret ``row``: what its profile allows, and of that, for a release run an env
    secret of its runtime's login alone, for a run of the Curator what its charter names (``allowed``)."""
    if not run.allows(row.name):
        return False
    if run.release:
        return row.kind == "env" and row.env_var in RELEASE_ENV_VARS
    return allowed is None or (row.kind, row.name) in allowed


def _choose(survey: _Survey) -> None:
    """Which git secret for each origin, and which repos need a GitHub token; the reason of each repo that nothing can
    cover. The env secrets are chosen when they are leased (``account_choice``)."""
    by_preference = sorted(survey.secrets, key=lambda secret: (not secret.on_worker, secret.name))
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
            # the owner's own first, then the longest prefix; a stable sort keeps this worker's first, then the name
            first = sorted(covering, key=lambda s: (s.share_id is not None, -len(normalize_origin(s.url_prefix))))
            survey.git[repo] = first[0]
        elif on_github is not None and survey.run.allows(profiles.GITHUB_APP):
            survey.github[repo] = on_github
        elif on_github is not None:
            survey.missing[repo] = (
                f"agent @{survey.run.profile} allows no token of the hub's GitHub App ({profiles.GITHUB_APP}), and no "
                f"git secret it allows covers {origin}"
            )
        elif survey.run.release:
            survey.missing[repo] = RELEASE_OFF_GITHUB.format(origin=origin)
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
        secret.lease_name,
        env_var=secret.env_var,
        url_prefix=secret.url_prefix,
        username=secret.username,
        value=value,
        expires_at=secret.expires_at,
    )


def _token_lease(lease_id: int, owner: str, token: str, expires_at: datetime, repo: str | None = None) -> Lease:
    """The lease of a GitHub token for the repos of ``owner`` on github.com, named github-app:<owner>; for its repo
    ``repo`` alone, a token K7 lets read only (lease_autonomy)."""
    return Lease(
        lease_id,
        "git",
        "github-app",
        f"github-app:{owner}",
        url_prefix=f"https://{GITHUB_HOST}/{owner}" + (f"/{repo}" if repo else ""),
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
    survey.env = await choose_accounts(
        conn, survey.run, survey.secrets, {secret_id for secret_id, _ in held}, survey.ended
    )
    chosen: list[tuple[_Secret, str]] = [(secret, secret.env_var) for secret in survey.env]
    for secret in dict.fromkeys(survey.git.values()):
        origins = [survey.origins[repo] for repo, found in survey.git.items() if found == secret]
        chosen.append((secret, _origins_text(normalize_origin(origin) for origin in origins)))
    leases = []
    for secret, target in chosen:
        try:
            aad = (
                secret_aad(secret.owner_id, secret.name, secret.kind)
                if secret.agent is None
                else agent_secret_aad(secret.profile_id, secret.name)
            )
            value = sealer.open(secret.sealed, aad)
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
                share_id=secret.share_id,
                **survey.run.lease_columns(),
            )
        else:  # a refresh gave the secret a new end: the lease the run holds follows it
            await conn.execute(update(cl).where(cl.c.id == lease_id).values(expires_at=secret.expires_at))
        leases.append(_secret_lease(lease_id, secret, value))
        survey.leased[lease_id] = secret
    await end_unlent(conn, survey.run.id, worker_id, {s.share_id for s, _ in chosen if s.share_id is not None})
    return leases


async def _lease_tokens(
    conn: AsyncConnection, survey: _Survey, worker_id: int, sealer: Sealer, made: list[InstallationToken] | None
) -> list[Lease]:
    """A lease of each token ``made``, sealed under its own id; with none made, the tokens the run holds already that
    answer for its repos on github.com, the newest first."""
    needed = {
        normalize_origin(survey.origins[repo]): owner
        for repo, (owner, _) in survey.github.items()
        if repo not in survey.read_only  # a token the run holds may write
    }
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
            **survey.run.lease_columns(),
        )
        sealed = sealer.seal(token.token, lease_aad(lease_id))  # bound to the lease's id, so inserted first
        cl = tables.credential_leases
        await conn.execute(
            update(cl)
            .values(sealed_value=sealed.ciphertext, nonce=sealed.nonce, key_id=sealed.key_id)
            .where(cl.c.id == lease_id)
        )
        owner = survey.github[repos[0]][0] if repos else token.installation.account
        alone = survey.github[repos[0]][1] if repos and repos[0] in survey.read_only else None
        leases.append(_token_lease(lease_id, owner, token.token, token.expires_at, alone))
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


async def _ask_github(
    state, app: GitHubApp | None, survey: _Survey, curator: bool
) -> tuple[list[InstallationToken] | None, dict[str, str]]:
    """Ask the App for a token for each repo of the run on github.com: (the tokens it made, None without an App;
    repo -> why the repo gets none, GitHub's reason or the App the hub lacks)."""
    if app is None:
        unset = ", ".join(state.config.curator_app_missing() if curator else state.config.github_app_missing())
        refusal = NO_CURATOR_APP if curator else NO_APP
        return None, {repo: refusal.format(missing=unset) for repo in survey.github}
    pusher = Pusher(survey.run.owner, survey.run.owner_github_id)
    kind = survey.run.kind
    tokens, reasons = await lease_autonomy.read_tokens(app, pusher, survey)  # a token each repo K7 lets read only
    writes = [where for repo, where in survey.github.items() if repo not in survey.read_only]
    if not writes:
        return tokens, reasons
    found = await app.tokens(writes, pusher, github_permissions(kind), workflows=github_workflows(kind))
    for repo, (owner, name) in survey.github.items():
        if f"{owner}/{name}" in found.missing and repo not in survey.read_only:
            reasons[repo] = found.missing[f"{owner}/{name}"]
    return [*found.tokens, *tokens], reasons


@worker_router.post("/runs/{run_id}/credentials", response_model=Credentials, responses=REFUSALS)
async def lease_credentials(request: Request, run_id: RunId, user: CurrentUser, response: Response) -> Credentials:
    """The leases of a run this worker holds: the owner's secrets for its project and a GitHub token for its repos on
    github.com, with the reason of each repo nothing covers."""
    state = request.app.state
    sealer: Sealer | None = state.sealer
    engine = state.engine
    async with engine.begin() as conn:
        _, survey = await survey_run(conn, user, run_id, lock=False, refreshable=True)
    ended = {secret.id: secret for secret in survey.secrets}  # with the OAuth accounts whose token has ended
    rejected: set[int] = set()
    if sealer is not None:  # before the leases are recorded: an access token near its end is traded for a new one
        members = [secret for secret in survey.secrets if secret.agent is None]  # an agent's accounts never sign in
        rejected = await refresh_due(engine, sealer, members)
    curator = survey.run.curator is not None
    app: GitHubApp | None = getattr(state, "curator_app", None) if curator else state.github_app
    made: list[InstallationToken] | None = None
    reasons: dict[str, str] = {}
    checked, unchecked = await lease_autonomy.before_lease(engine, state, app, survey)
    if sealer is not None and survey.github and not survey.reusable():
        made, reasons = await _ask_github(state, app, survey, curator)
    try:
        async with engine.begin() as conn:
            worker_id, survey = await survey_run(conn, user, run_id, lock=True)
            lease_autonomy.narrow(survey, unchecked, checked)
            if sealer is None:
                missing_key = ", ".join(state.config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
                leases: list[Lease] = []
                survey.missing = {repo: NO_KEY.format(missing=missing_key) for repo in survey.origins}
            else:
                if rejected:  # a refresh token the endpoint refused: the run leaves that account, as for a usage limit
                    seen = {secret.id for secret in survey.secrets}
                    left = [ended[i] for i in sorted(rejected) if i in ended and i not in seen]  # ended since
                    await fail_over_relogin(conn, survey.run, [*survey.secrets, *left], rejected)
                leases = await _lease_secrets(conn, survey, worker_id, sealer)
                leases += await _lease_tokens(conn, survey, worker_id, sealer, made)
                await _note_uncovered(conn, run_id, survey, leases, reasons)
            target = _lease_target(survey.run, leases, survey.leased)
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.CREDENTIAL_LEASE,
                target=target,
                project_id=survey.run.project_id,
            )
            await lease_autonomy.audited(conn, survey, user.user_id, user.token_id)
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
        "secrets": sorted({lease.name for lease in leases if lease.provider == "secret" and lease.name[0] != "@"}),
        "github_tokens": sum(lease.provider == "github-app" for lease in leases),
        "missing": len(missing),
    }
    log.info("credentials leased", extra=extra)
    response.headers.update(NO_STORE)
    repos = [
        RunRepoSource(
            repo=repo,
            origin=survey.origins[repo],
            path=survey.places[repo].path if repo in survey.places else None,
            project=survey.places[repo].project if repo in survey.places else None,
        )
        for repo in survey.origins
    ]
    return Credentials(
        leases=[CredentialLease.model_validate(lease.to_json()) for lease in leases], missing=missing, repos=repos
    )


async def _note_uncovered(
    conn: AsyncConnection, run_id: int, survey: _Survey, leases: list[Lease], reasons: dict[str, str]
) -> None:
    """Record, on ``survey``, why each repo on github.com no token of ``leases`` covers (GitHub's reason, from
    ``reasons``), then the shares the run was refused (``note_refusals``)."""
    for repo in survey.github:
        if not _covered(leases, survey.origins[repo]):
            survey.missing[repo] = reasons.get(repo) or (
                f"GitHub made no token for {'/'.join(survey.github[repo])}; ask again"
            )
    env_vars = {lease.env_var for lease in leases if lease.kind == "env"}
    missing = [survey.origins[repo] for repo in survey.missing]
    await note_refusals(conn, run_id, survey.refused, env_vars, missing)


def _names(values) -> str:
    return ",".join(sorted(set(values))) or "-"


def _ids(values) -> str:
    return ",".join(str(value) for value in sorted(set(values))) or "-"


def _lease_target(run: _Run, leases: list[Lease], leased: dict[int, _Secret]) -> str:
    """How a credential.lease audit row names an ask: the run, the secrets of its dispatcher by id, its own and those
    lent to it (their names are their owner's, and the audit is read by hub admins), the accounts of its agent by id
    with the agent's handle (`` agent-accounts=12@reviewer``, after the repos, only when there is one), the App's
    accounts and the repos."""
    secrets = [leased[lease.id] for lease in leases if lease.provider == "secret"]
    own = _ids(secret.id for secret in secrets if secret.agent is None)
    agents = ",".join(
        f"{secret.id}@{secret.agent}"
        for secret in sorted((s for s in secrets if s.agent is not None), key=lambda s: s.id)
    )
    accounts = _names(lease.name.partition(":")[2] for lease in leases if lease.provider == "github-app")
    named = f" agent-accounts={agents}" if agents else ""  # the row of a run without one reads as it always did
    return f"{run.target} secrets={own} github-app={accounts} repos={_names(run.repos)}{named}{run.profile_text()}"


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
