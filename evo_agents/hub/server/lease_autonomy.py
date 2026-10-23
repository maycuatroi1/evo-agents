"""K7 at the lease (plan autonomy-levels, criterion a6): a run is leased only the credentials K7 of its principal
allows (``evo_agents.hub.server.credentials`` asks here). The runtime's own sign-in, an env secret of a model provider
(``account_providers.category_of``), is no part of K7: every run that runs gets it.

- auto (no condition), and a run without an autonomy snapshot: as before.
- auto:run_branch: a GitHub token that writes only for a repo whose ruleset keeps the App off its default branch
  (``pulls.check_ruleset``, as the Curator's Builder is checked), a token that reads for every other repo; a git secret
  only for an origin on GitLab whose default branch no one may push to (gate 0 of the merge queue); no other env secret.
- auto:test_secrets: as run_branch, and the env secrets labelled ``environment=test`` (``hub secret set
  --environment test``).
- deny: no credential that writes: a token that reads for each repo on GitHub, no git secret, no env secret but the
  runtime's.

A git secret refused leaves its repo to the App when it is on GitHub, else missing with the reason. Each reason names
the class, the mode and the source, and a lease that withheld something writes the audit row ``autonomy.refused``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import account_providers, merge_gate, profiles
from evo_agents.hub.autonomy_levels import ACTING, DENY, TEST_SECRETS, Choice, refusal
from evo_agents.hub.credentials import GITHUB_READ_PERMISSIONS, TEST_ENVIRONMENT, github_repo
from evo_agents.hub.server import audit, autonomy_ledger, run_autonomy
from evo_agents.hub.server.forge import forge_token
from evo_agents.hub.server.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.github_app import GitHubApp, InstallationToken, Pusher
from evo_agents.hub.server.gitlab import GitLabRefused, GitLabUnavailable

log = logging.getLogger(__name__)

K7 = "K7"


async def k7(conn: AsyncConnection, run_id: int, lending: Iterable[int] = ()) -> Choice | None:
    """K7 of run ``run_id`` now (``run_autonomy.recheck``), under the credential shares ``lending`` lend it besides
    those it holds a lease under (source 6); None for a run without a snapshot."""
    found = await run_autonomy.recheck(conn, run_id, lending=lending)
    return None if found is None else found.classes[K7]


def limits(choice: Choice | None) -> bool:
    """Whether ``choice`` withholds anything: deny, or a mode that acts under a condition."""
    return choice is not None and (choice.mode not in ACTING or choice.condition is not None)


def runtime_secret(kind: str, env_var: str | None) -> bool:
    """An env secret of the runtime's own sign-in: no part of K7."""
    return (
        kind == "env"
        and bool(env_var)
        and account_providers.category_of(account_providers.provider_of(env_var)) == "model"
    )


def secret_allowed(choice: Choice | None, kind: str, env_var: str | None, environment: str | None) -> bool:
    """Whether K7 ``choice`` lets a run be leased a secret of ``kind``: a git secret is kept for ``Checked.apply``,
    which names why its origin gets none; an env secret is the runtime's, or labelled test under test_secrets."""
    if not limits(choice) or kind != "env" or runtime_secret(kind, env_var):
        return True
    return choice.mode in ACTING and choice.condition == TEST_SECRETS and environment == TEST_ENVIRONMENT


def why(choice: Choice, what: str) -> str:
    return f"{what}: {refusal(K7, choice.mode if choice.condition is None else f'{choice.mode}:{choice.condition}', choice)}"


@dataclass
class Checked:
    """What the hub found of the run's repos before the lease: repos whose origin forbids a push to its default
    branch on GitLab (``git_ok``), repos on GitHub whose ruleset keeps the App off it (``write_ok``), and why each other
    repo was not."""

    git_ok: set[str] = field(default_factory=set)
    write_ok: set[str] = field(default_factory=set)
    reasons: dict[str, str] = field(default_factory=dict)

    def apply(self, survey) -> None:
        """Narrow ``survey`` (``credentials.survey_run``) to what its K7 allows, as the module says."""
        choice = survey.k7
        if not limits(choice):
            return
        for repo in [repo for repo in survey.git if choice.mode == DENY or repo not in self.git_ok]:
            del survey.git[repo]
            refused = why(choice, self.reasons.get(repo) or f"no git secret for {survey.origins[repo]}")
            where = github_repo(survey.origins[repo])
            if where is not None and survey.run.allows(profiles.GITHUB_APP) and not survey.run.release:
                survey.github[repo] = where
            else:
                survey.missing[repo] = refused
            survey.withheld.append(f"git:{repo}")
        for repo, (owner, name) in survey.github.items():
            if choice.mode == DENY or repo not in self.write_ok:
                what = self.reasons.get(repo) or f"a token that writes to {owner}/{name}"
                survey.read_only[repo] = why(choice, what + "; a token that reads instead")
                survey.withheld.append(f"github:{owner}/{name}")


async def check(engine: AsyncEngine, state, app: GitHubApp | None, survey, checked_already: bool) -> Checked:
    """Check, before the lease, the origins of a run whose K7 has a condition (``checked_already``: the Curator's
    Builder, whose rulesets ``rulesets_now`` checked with the Curator's App)."""
    found = Checked()
    choice = survey.k7
    if not limits(choice) or choice.mode not in ACTING:
        return found
    for repo in survey.git:
        reason = await _push_forbidden(engine, state, survey.run.project_id, survey.origins[repo])
        if reason is None:
            found.git_ok.add(repo)
        else:
            found.reasons[repo] = reason
    on_github = {repo: github_repo(survey.origins[repo] or "") for repo in [*survey.git, *survey.github]}
    for repo, where in on_github.items():
        if where is None or repo in found.git_ok:
            continue
        owner, name = where
        if checked_already and repo in survey.github:
            found.write_ok.add(repo)
            continue
        reason = await _ruleset_keeps_off(app, owner, name)
        if reason is None:
            found.write_ok.add(repo)
        else:
            found.reasons[repo] = reason
    return found


async def before_lease(engine: AsyncEngine, state, app: GitHubApp | None, survey) -> tuple[Checked, dict[str, str]]:
    """What the hub checks of the run's origins before it asks GitHub for a token, unlocked: the rulesets of a Builder
    of the Curator (``rulesets_now``; repo -> why one gets no token) and what K7 allows (``check``); ``survey`` narrowed
    to both. A hub that keeps no secrets leases nothing: it checks nothing."""
    unchecked: dict[str, str] = {}
    if state.sealer is None:
        return Checked(), unchecked
    builder = survey.run.curator is not None and survey.run.curator.role == "builder"
    if builder and app is not None and survey.github:
        unchecked = await rulesets_now(engine, app, survey)
        drop_github(survey, unchecked)
    checked = await check(engine, state, app, survey, builder)
    checked.apply(survey)
    return checked, unchecked


def narrow(survey, unchecked: dict[str, str], checked: Checked) -> None:
    """Narrow the survey read again, locked, to what ``before_lease`` found."""
    drop_github(survey, unchecked)
    checked.apply(survey)


async def rulesets_now(engine: AsyncEngine, app: GitHubApp, survey) -> dict[str, str]:
    """For a Builder of the Curator: check again, with the Curator's App, the ruleset of each of its repos on GitHub,
    and keep what each check found; repo -> why a repo whose ruleset does not keep the Curator off, or could not be
    checked now, gets no token."""
    from evo_agents.hub.server.protection import store_check  # it reads runs through the routes that import this one
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
            await store_check(conn, survey.run.project_id, repo, f"{owner}/{name}", found, None)
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


def drop_github(survey, refused: dict[str, str]) -> None:
    """Take the repos of ``refused`` out of those that get a GitHub token, each missing with its reason."""
    for repo, why in refused.items():
        survey.github.pop(repo, None)
        survey.missing[repo] = why


async def _push_forbidden(engine: AsyncEngine, state, project_id: int, origin: str | None) -> str | None:
    """Why the default branch of ``origin`` may not be known to forbid every push (gate 0); None when it does."""
    async with engine.begin() as conn:
        token = await forge_token(conn, state.sealer, project_id, origin) if origin else None
    if token is None:
        return f"the hub cannot tell that the default branch of {origin} forbids a push: no forge secret on GitLab"
    path = token.project.path
    try:
        async with token.gitlab(transport=getattr(state, "gitlab_transport", None)) as gl:
            branch = (await gl.project(path)).default_branch
            protected = await gl.protected_branch(path, branch) if branch else None
    except (GitLabUnavailable, GitLabRefused) as exc:
        return f"the hub could not read the default branch of {path} on GitLab now ({exc})"
    levels = protected.push_access_levels if protected is not None else ()
    if not levels or any(level != merge_gate.NO_ONE for level in levels):
        return f"someone may push to {branch or 'the default branch'} of {path}"
    return None


async def _ruleset_keeps_off(app: GitHubApp | None, owner: str, name: str) -> str | None:
    from evo_agents.hub.server.pulls import check_ruleset  # it reads runs through the routes that import this one

    if app is None:
        return f"this hub has no GitHub App to check the ruleset of {owner}/{name}"
    try:
        found = await check_ruleset(app, owner, name)
    except GitHubUnavailable as exc:
        return f"the hub could not check the ruleset of {owner}/{name} now ({exc})"
    if not found.protected:
        return f"the ruleset of {owner}/{name} does not keep the hub's App off its default branch ({found.reason})"
    return None


async def read_tokens(app: GitHubApp, pusher: Pusher, survey) -> tuple[list[InstallationToken], dict[str, str]]:
    """A token that reads for each repo of ``survey.read_only``, one repo each: (the tokens, repo -> why one has
    none)."""
    tokens, reasons = [], {}
    for repo in survey.read_only:
        owner, name = survey.github[repo]
        try:
            found = await app.tokens([(owner, name)], pusher, dict(GITHUB_READ_PERMISSIONS), workflows=False)
        except (GitHubUnavailable, GitHubRefused) as exc:
            reasons[repo] = f"GitHub made no token that reads {owner}/{name}: {exc}"
            continue
        tokens += found.tokens
        if f"{owner}/{name}" in found.missing:
            reasons[repo] = found.missing[f"{owner}/{name}"]
    return tokens, reasons


def read_only(token: InstallationToken) -> bool:
    """Whether ``token`` only reads: its lease answers for its one repo."""
    return (token.permissions or {}).get("contents") != "write"


async def audited(conn: AsyncConnection, survey, actor_id: int | None, token_id: int | None) -> None:
    """The audit row of a lease that withheld something of K7 (``survey.withheld``), and the decision of K7 on the
    ledger S1 (``autonomy_ledger``) for a run with a snapshot."""
    choice = survey.k7
    if choice is None:
        return
    mode = choice.mode if choice.condition is None else f"{choice.mode}:{choice.condition}"
    result = f"withheld {','.join(sorted(set(survey.withheld)))}" if survey.withheld else "granted"
    await autonomy_ledger.record(conn, survey.run.id, K7, mode, result, "lease", token_id=token_id)
    if not survey.withheld:
        return
    await audit.record(
        conn,
        actor_id=actor_id,
        token_id=token_id,
        action=audit.AUTONOMY_REFUSED,
        target=f"run:{survey.run.id} {K7} lease {' '.join(sorted(set(survey.withheld)))} mode={mode} "
        f"source={choice.source}",
        project_id=survey.run.project_id,
    )
