"""The leases of a run: what the worker holding a run gets of its owner's secrets and of the hub's GitHub App, and how
the run gives them back (``docs/credentials.md``).

POST /v1/worker/runs/{id}/credentials, with the worker's token and the protocol header as on every worker route, asks
for the leases of a run the worker holds, in one of ``runs.HELD_STATES`` (``waiting`` of a plan run among them); any
other run, another worker's or one that ended, is 404 as for a state report. The hub takes the run's repos (its repo
for a run of one step, its repos for a plan run, and for a chat run those it started with and those its agent opened
since, ``chats.routes.agents.open_repo``) and their origins in project_repos (the run's project's, else another project's the
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
other origins only the git secret the charter names (``git_secret``) is leased, for a review or judge run also those
it lists to read with (``read_git_secrets``), and of the env secrets only those the charter lists (``env_secrets``). A KG run on a node of the shared KG pool whose charter gives no model login, as a
project fed by uploads alone has none, is leased the pool owner's own logins bound to that pool instead
(``share_leases.kg_pool_logins``). Each ask of a Builder of the Curator checks again, with the Curator's App, that the
ruleset of each of its repos on GitHub still keeps that App off the default branch (``pulls.check_ruleset``, kept as the
repo's last check): a repo whose check does not pass, or cannot be made now, gets no token, and its reason says why.

A release run gets its runtime's login alone of its owner's secrets (``credentials.RELEASE_ENV_VARS``), no git secret,
and for its repo on GitHub a token of the workers' App without workflows (``credentials.github_workflows``), which its
worker hands the daemon's git alone (``evo_agents.worker.credentials.RunCredentials.guarded``).

A command run (``evo_agents.hub.command_runs``) gets what any run of its owner over its repo gets, K7 of its principal
narrowing it as any run's, less every model account: no env secret of MODEL_ENV_VARS, no account of a pool and no KG
pool's login, since its shell calls no model.

A run's agent profile (``evo_agents.hub.profiles``), at the version the run went under, narrows that further: of the
secrets above, only those its credential allowlist names are leased, and a token of the hub's GitHub App only when it
names ``github-app`` (a repo it would cover is missing, with the profile as the reason). Each lease records the profile
and version it was handed out under; the credential.lease audit row names them (``profile=@<handle>:v<n>``) unless
the profile is the project's default.

The model accounts of a run under an agent (never a run of the Curator) are those of its account pool alone
(``pool_accounts``), chosen as ``account_choice`` says by DEFAULT_POLICY, weight and priority: never a model account of
the dispatcher's or of a share, whatever the profile's credential axis says; when every account of the pool is paused,
cooling or past its end, the run waits in the queue (``account_choice`` says why in an event). Every other variable is
the dispatcher's as above. An account's lease is named ``@<handle>:<name>`` (``credentials.agent_lease_name``), and it
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

A run whose owner lets it run as them (``run_access``) is also leased its own token of the hub, ``evr_...``, of kind
hub: the same token at each ask while the lease is out, a new one after; the credential.lease row names it by id
(``run-access=token:<id>``).

GET /v1/projects/{p}/runs/{id}/credentials, the owner's view of a run's leases, is in ``run_leases``.

The owner must still hold writer on the run's project when the worker asks: once the grant is gone or lowered to
reader, the ask is 403, and the admin route that took it gave back the leases still out of every run the member
dispatched in the project (``end_member_leases``, from ``admin.grant`` and ``admin.revoke``). So once the owner's
primary seat in the org chart ended, on every project (``org_expiry.take_back``).

The hub gives leases back itself (``end_leases``) when a run leaves the held states (``run_state.move_run``: it ends,
waits in review, or is parked) and when a worker is revoked (``workers.end_worker``, every lease of the worker). Those
rows are marked revoked in the transaction of the move, and their GitHub tokens revoked at GitHub by ``revoke_tokens``
once it commits: by the route that made the move, and on every pass of the reaper (``run_reaper.recover_runs``), which
also takes the tokens GitHub failed to answer for. ``drop_expired`` (from the daily ``run_reaper.prune_run_events``)
drops the sealed values of tokens past their end, which GitHub no longer takes. No log line, audit row or error holds a
value.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.credentials import (
    GITHUB_HOST,
    Lease,
    github_permissions,
    github_pull_requests,
    github_workflows,
    matches,
    normalize_origin,
)
from evo_agents.hub.server import audit, gateway_leases, gateway_sync, run_access
from evo_agents.hub.server.account_choice import choose_accounts, fail_over_relogin
from evo_agents.hub.server.account_refresh import refresh_due
from evo_agents.hub.server.autonomy.service import lease as lease_autonomy
from evo_agents.hub.server.base.errors import NotFound
from evo_agents.hub.server.credentials.models.leases import (
    GITHUB_GIT_USERNAME,
    NO_APP,
    NO_CURATOR_APP,
    NO_KEY,
    Credentials,
    GivenBack,
    MissingOrigin,
    RunRepoSource,
)
from evo_agents.hub.server.credentials.models.sealing import (
    Sealer,
    Unsealable,
    agent_secret_aad,
    lease_aad,
    secret_aad,
)
from evo_agents.hub.server.credentials.repository import leases as repository
from evo_agents.hub.server.credentials.service.survey import LeaseRun, LeaseSecret, Survey, origins_text, survey_run
from evo_agents.hub.server.github import GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, InstallationToken, Pusher
from evo_agents.hub.server.lease_ending import end_leases, revoke_tokens, revoker
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.share_leases import end_unlent, note_refusals

log = logging.getLogger(__name__)


# Asking


def _secret_lease(lease_id: int, secret: LeaseSecret, value: str) -> Lease:
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


async def _lease_secrets(conn: AsyncConnection, survey: Survey, worker_id: int, sealer: Sealer) -> list[Lease]:
    """A lease of each secret ``survey`` chose, the one the run holds already when there is one; a secret that does not
    open with the hub's key is left out, and the repos it was to answer for are missing."""
    held = await repository.held_secret_leases(conn, survey.run.id, worker_id)
    survey.env = await choose_accounts(  # pyright: ignore[reportAttributeAccessIssue]
        conn,
        survey.run,  # pyright: ignore[reportArgumentType]
        survey.secrets,  # pyright: ignore[reportArgumentType]
        {secret_id for secret_id, _ in held},
        survey.ended,
    )
    chosen: list[tuple[LeaseSecret, str]] = [(secret, secret.env_var) for secret in survey.env]  # pyright: ignore[reportAssignmentType]
    for secret in dict.fromkeys(survey.git.values()):
        origins = [survey.origins[repo] for repo, found in survey.git.items() if found == secret]
        chosen.append((secret, origins_text(normalize_origin(origin) for origin in origins)))  # pyright: ignore[reportArgumentType]
    leases = []
    for secret, target in chosen:
        try:
            aad = (
                secret_aad(secret.owner_id, secret.name, secret.kind)
                if secret.owner_id is not None
                else agent_secret_aad(secret.profile_id, secret.name)  # pyright: ignore[reportArgumentType]
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
            lease_id = await repository.insert_lease(
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
            await repository.follow_end(conn, lease_id, secret.expires_at)
        leases.append(_secret_lease(lease_id, secret, value))
        survey.leased[lease_id] = secret
    await end_unlent(conn, survey.run.id, worker_id, {s.share_id for s, _ in chosen if s.share_id is not None})
    return leases


async def _lease_tokens(
    conn: AsyncConnection, survey: Survey, worker_id: int, sealer: Sealer, made: list[InstallationToken] | None
) -> list[Lease]:
    """A lease of each token ``made``, sealed under its own id; with none made, the tokens the run holds already that
    answer for its repos on github.com, the newest first."""
    needed = {
        normalize_origin(survey.origins[repo]): owner  # pyright: ignore[reportArgumentType]
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
        lease_id = await repository.insert_lease(
            conn,
            run_id=survey.run.id,
            worker_id=worker_id,
            secret_id=None,
            provider="github-app",
            target=origins_text(normalize_origin(survey.origins[repo]) for repo in repos),  # pyright: ignore[reportArgumentType]
            external_id=str(token.installation.id),
            expires_at=token.expires_at,
            **survey.run.lease_columns(),
        )
        sealed = sealer.seal(token.token, lease_aad(lease_id))  # bound to the lease's id, so inserted first
        await repository.seal_lease(conn, lease_id, sealed.ciphertext, sealed.nonce, sealed.key_id)
        owner = survey.github[repos[0]][0] if repos else token.installation.account
        alone = survey.github[repos[0]][1] if repos and repos[0] in survey.read_only else None
        leases.append(_token_lease(lease_id, owner, token.token, token.expires_at, alone))
    return leases


def _covered(leases: list[Lease], origin: str) -> bool:
    return any(lease.kind == "git" and matches(lease.url_prefix, origin) for lease in leases)  # pyright: ignore[reportArgumentType]


async def _revoke_made(app: GitHubApp | None, made: list[InstallationToken] | None) -> None:
    """Revoke the tokens an ask made and could not record, for a run it then found no longer held or for a failure of
    the database; GitHub failing leaves them to expire."""
    for token in made or []:
        try:
            await app.revoke(token.token)  # pyright: ignore[reportOptionalMemberAccess]
        except GitHubUnavailable as exc:
            log.warning("a GitHub token made for a run that ended is left to expire", extra={"why": str(exc)})


async def _ask_github(
    state, app: GitHubApp | None, survey: Survey, curator: bool
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
    found = await app.tokens(
        writes,
        pusher,
        github_permissions(kind),
        workflows=github_workflows(kind),
        pull_requests=github_pull_requests(kind),
    )
    for repo, (owner, name) in survey.github.items():
        if f"{owner}/{name}" in found.missing and repo not in survey.read_only:
            reasons[repo] = found.missing[f"{owner}/{name}"]
    return [*found.tokens, *tokens], reasons


async def _lease_all(
    conn: AsyncConnection, run_id: int, survey: Survey, worker_id: int, sealer: Sealer, made, gateway, reasons, root
):
    """Record the leases of the run: its secrets, its GitHub tokens, its gateway token and its token of the hub;
    returns them with the id of that token of the hub, None when the run does not run as its owner."""
    leases = await _lease_secrets(conn, survey, worker_id, sealer)
    leases += await _lease_tokens(conn, survey, worker_id, sealer, made)
    leases += await gateway_leases.record(conn, gateway, survey.run, worker_id, sealer)
    await _note_uncovered(conn, run_id, survey, leases, reasons)
    hub, token_id = await run_access.lease(conn, survey.run, worker_id, sealer, root)
    return leases + hub, token_id


def _credentials(run_id: int, worker_id: int, survey: Survey, leases: list[Lease]) -> Credentials:
    """What the worker gets, logged without the values."""
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
    repos = [
        RunRepoSource(
            repo=repo,
            origin=survey.origins[repo],
            path=survey.places[repo].path if repo in survey.places else None,
            project=survey.places[repo].project if repo in survey.places else None,
        )
        for repo in survey.origins
    ]
    return Credentials(leases=[lease.to_json() for lease in leases], missing=missing, repos=repos)  # pyright: ignore[reportArgumentType]


async def _note_uncovered(
    conn: AsyncConnection, run_id: int, survey: Survey, leases: list[Lease], reasons: dict[str, str]
) -> None:
    """Record, on ``survey``, why each repo on github.com no token of ``leases`` covers (GitHub's reason, from
    ``reasons``), then the shares the run was refused (``note_refusals``)."""
    for repo in survey.github:
        if not _covered(leases, survey.origins[repo]):  # pyright: ignore[reportArgumentType]
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


def _lease_target(run: LeaseRun, leases: list[Lease], leased: dict[int, LeaseSecret]) -> str:
    """How a credential.lease audit row names an ask: the run, the secrets of its dispatcher by id, its own and those
    lent to it (their names are their owner's, and the audit is read by hub admins), the accounts of its agent by id
    with the agent's handle (`` agent-accounts=12@reviewer``, after the repos, only when there is one), the App's
    accounts and the repos."""
    secrets = [leased[lease.id] for lease in leases if lease.provider == "secret"]
    own = _ids(secret.id for secret in secrets if secret.agent is None and secret.pool is None)
    agents = ",".join(
        f"{secret.id}@{secret.agent or 'pool.' + str(secret.pool)}"
        for secret in sorted((s for s in secrets if s.agent is not None or s.pool is not None), key=lambda s: s.id)
    )
    accounts = _names(lease.name.partition(":")[2] for lease in leases if lease.provider == "github-app")
    named = f" agent-accounts={agents}" if agents else ""  # the row of a run without one reads as it always did
    return f"{run.target} secrets={own} github-app={accounts} repos={_names(run.repos)}{named}{run.profile_text()}"


async def lease(app, user: Principal, run_id: int, root: str) -> Credentials:
    """The leases of run ``run_id`` for the worker of ``user`` (POST .../credentials, see the module); ``root`` is the
    hub's URL. GitHub is asked between the two transactions it opens, never while the run's row is locked."""
    state = app.state
    sealer: Sealer | None = state.sealer
    engine = state.engine
    async with engine.begin() as conn:
        asking, survey = await survey_run(conn, user, run_id, lock=False, refreshable=True)
    ended = {secret.id: secret for secret in survey.secrets}  # with the OAuth accounts whose token has ended
    rejected: set[int] = set()
    if sealer is not None:  # before the leases are recorded: an access token near its end is traded for a new one
        # a member's own accounts, in a pool or not, sign in and refresh; an agent's own never sign in
        members = [secret for secret in survey.secrets if secret.agent is None and secret.owner_id is not None]
        rejected = await refresh_due(engine, sealer, members)
    curator = survey.run.curator is not None
    github: GitHubApp | None = getattr(state, "curator_app", None) if curator else state.github_app
    made: list[InstallationToken] | None = None
    reasons: dict[str, str] = {}
    checked, unchecked = await lease_autonomy.before_lease(engine, state, github, survey)
    if sealer is not None and survey.github and not survey.reusable():
        made, reasons = await _ask_github(state, github, survey, curator)
    token_id: int | None = None  # the run's token of the hub, when its owner lets it run as them (``run_access``)
    gateway = None if sealer is None else await gateway_leases.prepare(app, engine, survey.run, asking, root)
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
                    await fail_over_relogin(conn, survey.run, [*survey.secrets, *left], rejected)  # pyright: ignore[reportArgumentType]
                leases, token_id = await _lease_all(
                    conn, run_id, survey, worker_id, sealer, made, gateway, reasons, root
                )
            target = _lease_target(survey.run, leases, survey.leased) + (gateway.audit_text() if gateway else "")
            target += "" if token_id is None else f" run-access=token:{token_id}"
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
        await _revoke_made(github, made)
        await gateway_leases.undo(app, gateway)
        raise
    return _credentials(run_id, worker_id, survey, leases)


async def give_back(app, user: Principal, run_id: int) -> GivenBack:
    """Give back every lease the worker of ``user`` holds of run ``run_id`` (DELETE .../credentials, see the module);
    404 for a run it never held."""
    state = app.state
    async with state.engine.begin() as conn:
        worker_id = (await worker_of(conn, user))[0]
        row = await repository.run_worker(conn, run_id)
        if row is None or row.worker_id != worker_id:
            raise NotFound(f"this worker never held run {run_id}")
        revoked = await end_leases(
            conn, run_id=run_id, worker_id=worker_id, actor_id=user.user_id, token_id=user.token_id, by="worker"
        )
    await revoke_tokens(state.engine, state.sealer, revoker(state), run_id=run_id)
    gateway_sync.poke(app)  # its key of the gateway, if any, is taken back by the next round
    log.info("credentials given back", extra={"run_id": run_id, "worker_id": worker_id, "revoked": revoked})
    return GivenBack(revoked=revoked)
