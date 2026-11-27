"""What a run may be leased (``service.leases`` asks here, and so do ``service.resolver``, ``rate_limits`` and the
account rules): the run a worker holds, its owner's secrets for its project, those lent to it, its agent's pool
accounts, the GitHub tokens it holds, narrowed by its profile, the Curator's charter and K7 of its principal, and which
git secret or GitHub token answers for each of its repos (see ``service.leases``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from evo_agents.hub import command_runs, profiles, releases, runs
from evo_agents.hub.account_providers import MODEL_ENV_VARS
from evo_agents.hub.credentials import RELEASE_ENV_VARS, agent_lease_name, github_repo, matches, normalize_origin
from evo_agents.hub.server.accounts.repository.pool_runs import pool_env_accounts
from evo_agents.hub.server.autonomy.service import lease as lease_autonomy
from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.credentials.models.leases import MAX_TARGET_CHARS, RELEASE_OFF_GITHUB
from evo_agents.hub.server.credentials.models.sealing import Sealed
from evo_agents.hub.server.credentials.repository import leases as repository
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.repo_origins import RepoOrigin, repo_origins
from evo_agents.hub.server.runs.models import NOT_HELD
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.share_leases import Refusal, kg_pool_logins, own_secrets, shared_secrets

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from evo_agents.hub.autonomy_levels import Choice
    from evo_agents.hub.server.changes.models.views import CuratorPolicy

# What a run may get


@dataclass(frozen=True)
class LeaseRun:
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
    as_owner: bool = False  # its owner lets it call the hub as them (``run_access``)
    account_pool_id: int | None = None  # its profile's account pool (``account_pools``)
    account_pool: str | None = None  # that pool's name

    @property
    def agent_owned(self) -> bool:
        """Whether the run goes under an agent with a name, whose accounts it is leased (see the module): not a run of
        the Curator, not the project's default profile, which has none."""
        return self.profile_id is not None and not self.profile_default and self.curator is None

    @property
    def pooled(self) -> bool:
        """Whether the run goes under an agent, whose account pool alone gives it model accounts (see the module): not
        a run of the Curator, which has its charter's."""
        return self.profile_id is not None and self.curator is None

    @property
    def target(self) -> str:
        return run_target(self.project, self.plan_id, self.step_key, self.id)

    @property
    def release(self) -> bool:
        """A release run: leased its runtime's login and a token of the workers' App alone (see the module)."""
        return self.kind == releases.RELEASE_KIND

    @property
    def command(self) -> bool:
        """A command run (``evo_agents.hub.command_runs``): its shell calls no model, so no model account is leased."""
        return self.kind == command_runs.KIND

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
class LeaseSecret:
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
    for_run: bool = False  # that share is for this run alone: an admin gave its value for it (``credential_answers``)
    agent: str | None = None  # the handle of the agent profile that owns it (``agent_accounts``); None: a member's
    profile_id: int | None = None  # that profile
    pool: str | None = None  # the account pool it is leased from (``account_pools``); None: not from one

    @property
    def lease_name(self) -> str:
        """The name its lease carries: the secret's, or ``@<handle>:<name>`` for an agent's account, or
        ``@pool.<pool>:<name>`` for a member of an account pool, which no secret of a member can have, so no two leases
        of an answer share a name."""
        if self.pool is not None:
            return f"@pool.{self.pool}:{self.name}"
        return self.name if self.agent is None else agent_lease_name(self.agent, self.name)


@dataclass(frozen=True)
class HeldToken:
    """A GitHub token the run holds already, with GITHUB_TOKEN_REFRESH_SECONDS or more left."""

    id: int
    origins: frozenset[str]  # what it answers for, normalized
    expires_at: datetime
    sealed: Sealed


@dataclass
class Survey:
    run: LeaseRun
    origins: dict[str, str | None]  # repo -> origin as registered, in the run's order
    secrets: list[LeaseSecret]
    tokens: list[HeldToken]
    places: dict[str, RepoOrigin] = field(default_factory=dict)  # repo -> where its origin and path come from
    env: list[LeaseSecret] = field(default_factory=list)
    ended: tuple[str, ...] = ()  # variables whose accounts of the agent all ended: leased from no one
    git: dict[str, LeaseSecret] = field(default_factory=dict)  # repo -> the secret that answers for its origin
    github: dict[str, tuple[str, str]] = field(default_factory=dict)  # repo -> (owner, repo) on github.com
    missing: dict[str, str] = field(default_factory=dict)  # repo -> why nothing covers its origin
    k7: Choice | None = None  # K7 of the run's principal now (lease_autonomy); None for a run without a snapshot
    read_only: dict[str, str] = field(default_factory=dict)  # repo on github.com -> why K7 gives it a token that reads
    withheld: list[str] = field(default_factory=list)  # what K7 kept from the run, for the audit row
    refused: list[Refusal] = field(default_factory=list)  # the shares that lent the run nothing, and why
    leased: dict[int, LeaseSecret] = field(default_factory=dict)  # lease id -> the secret leased under it

    def reusable(self) -> bool:
        """Whether the GitHub tokens the run holds cover every repo that needs one; never under a K7 that reads."""
        if self.read_only:
            return False
        held = set().union(*(token.origins for token in self.tokens)) if self.tokens else set()
        return all(normalize_origin(self.origins[repo]) in held for repo in self.github)  # pyright: ignore[reportArgumentType]


def run_repos(kind: str, repo: str | None, repos) -> tuple[str, ...]:
    if kind != "step":  # a plan run's repos, or a review run's
        names = [entry.get("repo") for entry in repos or [] if isinstance(entry, dict)]
    else:
        names = [repo]
    return tuple(dict.fromkeys(name for name in names if isinstance(name, str) and name))


async def held_run(conn: AsyncConnection, worker_id: int, run_id: int, *, lock: bool) -> LeaseRun:
    """The run ``run_id`` that worker ``worker_id`` holds, its row locked with ``lock``; 404 for any other. 403 when
    the member who dispatched it no longer holds writer on its project; with ``lock`` their grant's row is held too,
    so a grant taken away meanwhile waits for the leases of this ask, and takes them back (``end_member_leases``)."""
    row = await repository.run_row(conn, run_id, lock=lock)
    if row is None or row.worker_id != worker_id or row.state not in runs.HELD_STATES:
        raise NotFound(NOT_HELD.format(id=run_id))
    role = await repository.owner_role(conn, row.dispatched_by, row.project_id, lock=lock)
    if not holds(role, "writer"):
        raise Forbidden(
            f"{row.owner}, who dispatched run {run_id}, no longer holds the writer role on project {row.project}: "
            "the run gets no credentials",
        )
    ended = await ended_refusal(conn, row.dispatched_by, row.owner, what=f"leasing run {run_id}", lock=lock)
    if ended is not None:
        raise Forbidden(ended)
    from evo_agents.hub.server.changes.service.policy import (
        curator_policy,
    )  # it reads runs through the routes that import this one

    return LeaseRun(
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
        repos=run_repos(row.kind, row.repo, row.repos),
        owner_github_id=row.github_id,
        profile_id=row.profile_id,
        profile_version=row.profile_version,
        profile=row.profile,
        profile_default=row.profile_default,
        credentials=row.credentials,
        profile_project_id=row.profile_project_id,
        account_pool_id=row.account_pool_id,
        account_pool=row.account_pool,
        principal_id=row.principal_id,
        as_owner=row.as_owner,
    )


def origins_text(origins) -> str:
    """The origins a lease answers for, as credential_leases.target keeps them: space-separated, within its limit."""
    kept: list[str] = []
    for origin in sorted(set(origins)):
        if len(" ".join([*kept, origin])) > MAX_TARGET_CHARS:
            break
        kept.append(origin)
    return " ".join(kept) or "-"


async def survey_run(
    conn: AsyncConnection, user: Principal, run_id: int, *, lock: bool, refreshable: bool = False
) -> tuple[int, Survey]:
    """(worker id, what run ``run_id`` may get) for the worker of ``user``, which must hold the run. The worker's row
    is locked, as on every worker route; with ``lock`` the run's and the secrets' rows are too, so the run cannot end
    and a secret cannot be deleted until the leases are recorded."""
    worker_id = (await worker_of(conn, user))[0]
    run = await held_run(conn, worker_id, run_id, lock=lock)
    places = await repo_origins(conn, run.project_id, run.owner_id, run.repos)
    rows = await repository.rows(
        conn, own_secrets(run.owner_id, run.project_id, worker_id, lock=lock, refreshable=refreshable)
    )
    refused: list[Refusal] = []
    if run.curator is None and not run.release:  # their charter and their runtime's login are their own
        shared, refused = await shared_secrets(conn, run, worker_id, lock=lock, refreshable=refreshable)
        rows += shared
    allowed = allowed_secrets(run.curator)
    rows = [row for row in rows if leasable(run, allowed, row)]
    if run.command:  # no model is called: no model account of the owner's, a share's or a pool's
        rows = [row for row in rows if not (row.kind == "env" and row.env_var in MODEL_ENV_VARS)]
    else:
        rows += await kg_pool_logins(conn, run, worker_id, rows, lock=lock, refreshable=refreshable)  # no charter's
    lending = [row.share_id for row in rows if getattr(row, "share_id", None) is not None]
    k7 = await lease_autonomy.k7(conn, run.id, lending)  # a share's level holds the run from the lease that lends it
    accounts = []
    if (
        run.pooled and not run.command
    ):  # the account pool's accounts alone: no model account of the dispatcher's or a share's
        rows = [row for row in rows if not (row.kind == "env" and row.env_var in MODEL_ENV_VARS)]
        if run.account_pool_id is not None:
            accounts = [
                row
                for row in await repository.rows(conn, pool_env_accounts(run.account_pool_id, lock=lock))
                if row.env_var in MODEL_ENV_VARS and (not run.release or row.env_var in RELEASE_ENV_VARS)
            ]
    withheld = [row for row in [*rows, *accounts] if not lease_autonomy.secret_allowed(k7, *row_k7(row))]
    rows = [row for row in rows if lease_autonomy.secret_allowed(k7, *row_k7(row))]
    accounts = [row for row in accounts if lease_autonomy.secret_allowed(k7, *row_k7(row))]
    claimed = {row.env_var for row in accounts}
    secrets = [
        LeaseSecret(
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
            for_run=bool(getattr(row, "for_run", False)),
        )
        for row in rows
        if not (row.kind == "env" and row.env_var in claimed)
    ]
    secrets += [
        LeaseSecret(
            id=row.id,
            name=row.name,
            kind=row.kind,
            env_var=row.env_var,
            url_prefix=None,
            username=None,
            sealed=Sealed(row.sealed, row.nonce, row.key_id),
            expires_at=row.expires_at,
            on_worker=True,
            owner_id=row.owner_id if run.pooled else None,
            agent=None if run.pooled else run.profile,
            profile_id=row.profile_id if run.pooled else run.profile_id,
            pool=run.account_pool if run.pooled else None,
        )
        for row in accounts
        if refreshable or not row.ended  # one that ended may be refreshed before the leases (``refresh_due``)
    ]
    tokens = [
        HeldToken(
            id=row.id,
            origins=frozenset(row.target.split()),
            expires_at=row.expires_at,
            sealed=Sealed(row.sealed_value, row.nonce, row.key_id),
        )
        for row in await repository.held_tokens(conn, run_id, worker_id)
    ]
    origins = {repo: places[repo].origin if repo in places else None for repo in run.repos}
    survey = Survey(run, origins, secrets, tokens, places, k7=k7, refused=refused)
    survey.withheld = [f"env:{row.name}" for row in withheld]
    survey.ended = tuple(sorted(claimed - {row.env_var for row in accounts if not row.ended}))
    choose(survey)
    return worker_id, survey


def row_k7(row) -> tuple[str, str | None, str | None]:
    """(kind, env var, environment) of a secret's row, what K7 reads of it (``lease_autonomy.secret_allowed``)."""
    return row.kind, row.env_var, row.environment


def allowed_secrets(policy: CuratorPolicy | None) -> set[tuple[str, str]] | None:
    """(kind, name) of the secrets a run of the Curator may be leased: the charter's git secret, for a review or judge
    run its read_git_secrets too (``CuratorPolicy.git_secrets``), and its env secrets; None for any other run, which
    may be leased all its owner bound."""
    if policy is None:
        return None
    allowed = {("env", name) for name in policy.env_secrets}
    allowed |= {("git", name) for name in policy.git_secrets}
    return allowed


def leasable(run: LeaseRun, allowed: set[tuple[str, str]] | None, row) -> bool:
    """Whether ``run`` may be leased the secret ``row``: what its profile allows, and of that, for a release run an env
    secret of its runtime's login alone, for a run of the Curator what its charter names (``allowed``)."""
    if not run.allows(row.name):
        return False
    if run.release:
        return row.kind == "env" and row.env_var in RELEASE_ENV_VARS
    return allowed is None or (row.kind, row.name) in allowed


def choose(survey: Survey) -> None:
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
        covering = [secret for secret in git if matches(secret.url_prefix, origin)]  # pyright: ignore[reportArgumentType]
        if covering:
            # the owner's own first, then the longest prefix; a stable sort keeps this worker's first, then the name
            first = sorted(covering, key=lambda s: (s.share_id is not None, -len(normalize_origin(s.url_prefix))))  # pyright: ignore[reportArgumentType]
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
        elif survey.run.curator is not None:
            survey.missing[repo] = curator_git_missing(survey.run.curator, survey.run.owner, survey.run.project, origin)
        else:
            survey.missing[repo] = (
                f"no git secret of {survey.run.owner} bound to project {survey.run.project} covers {origin}"
            )


def curator_git_missing(policy: CuratorPolicy, owner: str, project: str, origin: str) -> str:
    """Why a run of the Curator gets no git secret for ``origin``: the charter names none that covers it, live and
    bound to the project. A run of the Curator is leased only the git secrets its charter names
    (``CuratorPolicy.git_secrets``), never the owner's others, so the reason says which ones it looked at."""
    named = ", ".join(policy.git_secrets)
    looked = (
        f"of the git secrets the charter names for a {policy.role} ({named})" if named else "the charter names none"
    )
    return (
        f"a run of the Curator reads {origin} with a git secret of {owner} its charter names, live and bound to project "
        f"{project}, and {looked}"
        + (" none covers it" if named else "")
        + ": name one in git_secret, or for a review or judge run in read_git_secrets"
    )
