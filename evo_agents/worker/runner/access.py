"""What a run may reach: the leases it takes for its repos, and the preflight that checks them before its agent starts.

Once its leases are taken, a run clones each of its repos this worker has no checkout of (``clones``). A best-effort
repo (each repo of a task run or a chat run, an author run's but its harness) the run still has no checkout of, or
that the project lists no origin for, is left out of it, noted and named in its prompt (``leaves_out``); the run goes
on without it. Then, before anything is fetched or the agent starts, it checks what it needs
(``preflight``): the project lists an origin for each of its repos, git reads it (and pushes to it, for a repo the run
pushes) with what the run holds, a credential its agent has opens a pull request in each repo on GitHub whose branch
it lands through one it opens itself (``opens_pull``: a plan run's repo whose branch in the plan is not a default
branch), each program its verify commands call is on the run's PATH, and the environment its
claim names can run its agent here (``environment_problems``: a container engine that answers and the image, for a
plugin's environment), and for a run dispatched with browser, Chromium on this worker and node on the run's PATH
(``browser_problems``). The origins of the run's repos are asked several at a time (``parallel``). A check that does
not pass fails the run before the agent starts, with its cause (the report's ``failure_cause``: origin, credentials,
missing_tool or environment). The origin of a repo whose worktree the run goes on in (``fetched``: a chat run that
adopted the worktrees of the run before it in its session) is not asked: the fetch of that worktree's checkout is the
check that git reads it, failing the run with cause credentials or checkout (``origin.fetch``), and git is asked to
push only by the push itself.

The leases stay in the daemon's memory (``credentials.RunCredentials``): the run's git gets them through
``credentials.ticketed``, its agent through ``Agent.env``, each event goes through ``credentials.scrub`` before the
spool, and they are given back once the run ends here, whether it ended, was parked, or the daemon stops. A push of the
daemon that fails to authenticate on a leased origin takes the leases again once and pushes once more
(``RunCredentials.with_renewal``). The agent of a run of the Curator, or of a release run, never holds a push
credential (``credentials.RunCredentials.guarded``): only the daemon's own git gets the run's leases, through a ticket
of one command, and it pushes such a run's repo only with a lease that covers it, never with this machine's own
credentials.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING

from evo_agents.hub.credentials import github_repo
from evo_agents.worker import credentials, gitops, preflight
from evo_agents.worker.browser import install as browser_install
from evo_agents.worker.environments import Container
from evo_agents.worker.runner import clones, parallel
from evo_agents.worker.runner.common import RunFailed, cut
from evo_agents.worker.runner.transitions import MAX_ERROR_CHARS
from evo_agents.worker.runtimes import claude_home

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


def repo_names(repos: Iterable) -> list[str]:
    """The names among ``repos``, each once, in order."""
    return list(dict.fromkeys(repo for repo in repos if isinstance(repo, str) and repo))


async def take_credentials(
    run: Run, repos: Iterable, *, clone: Iterable | None = None, best_effort: bool = False
) -> None:
    """Take the run's leases for ``repos``, handed to the origins of their checkouts here (read several at a time,
    ``parallel.each``); then set up a checkout of each of ``clone`` (every one of ``repos`` when None) this worker has
    none of, through those leases (``clones.ensure``; with ``best_effort`` one that cannot be is left out)."""
    names = repo_names(repos)

    async def urls_of(name: str) -> list[str] | None:
        checkout = run.daemon.checkout_for(run.project, name)
        if checkout is not None:
            with contextlib.suppress(gitops.GitError, OSError):
                return await gitops.remote_urls(checkout)
        return None

    found = await parallel.each(run, names, urls_of)
    origins = {name: urls for name, urls in zip(names, found, strict=True) if urls is not None}
    await run.credentials.take(run.daemon.hub, origins, stop=run.daemon.hard_stop)
    await clones.ensure(run, names if clone is None else repo_names(clone), best_effort=best_effort)


def leaves_out(run: Run, name: str) -> str | None:
    """Why a best-effort repo ``name`` of the run (a task run's, a chat run's, an author run's but its harness) is left
    out of it, as the run goes on without it: this worker has no checkout of it (``clones.ensure`` could not set one
    up), or the project lists no origin for it, so no credential is leased for it. None when the run works in it. A
    required repo is never left out: the preflight fails on it (``check``)."""
    if run.daemon.checkout_for(run.project, name) is None:
        return "checkout"
    if name in run.credentials.unlisted:
        return "origin"
    return None


def left_out_notes(run: Run, missing: list[str], unlisted: list[str]) -> None:
    """Note the best-effort repos the run goes on without, by why (``leaves_out``)."""
    if missing:
        run.note(f"No checkout of {', '.join(missing)} on this worker: the agent works without them.")
    if unlisted:
        run.note(
            f"Project {run.project} lists no origin for {', '.join(unlisted)}, so no credential is leased for them: "
            "the agent works without them."
        )


def left_out_lines(missing: list[str], unlisted: list[str]) -> list[str]:
    """What a run's prompt adds of the best-effort repos it goes on without."""
    extra = []
    if missing:
        extra += ["", f"Repos of the project this worker has no checkout of: {', '.join(missing)}."]
    if unlisted:
        extra += ["", f"Repos of the project it lists no origin for, left out of this run: {', '.join(unlisted)}."]
    return extra


def run_path(run: Run) -> str | None:
    """The PATH the run's verify commands run with: the agent's."""
    return run.agent.env().get("PATH")


async def check(
    run: Run,
    repos: Iterable,
    *,
    path: str | None,
    pushes: Collection[str] = (),
    pulls: Collection[str] = (),
    verify: Collection[str | tuple[str | None, str]] = (),
    hidden: list[str] | None = None,
    fetched: Collection[str] = (),
) -> None:
    """The preflight of the run (``preflight``): each of ``repos`` has an origin in the project and one git reads with
    what the run holds, and pushes to for those of ``pushes``; for those of ``pulls`` on GitHub that git reaches, a
    credential the agent has opens a pull request there (``opens_pull``), a guarded run's never asked; each program
    ``verify`` and ``hidden`` (a judge run's hidden checks) call is on ``path``, a verify being a command or a (step
    key, command) pair whose problem names the step. The origins are asked several at a time (``parallel.each``), each
    problem in the order of ``repos``; those of ``fetched`` are not asked, the fetch of their worktrees reads them.
    RunFailed with every problem found, the cause of the first: the agent does not start."""
    names = repo_names(repos)
    problems: list[preflight.Problem] = []
    unlisted = run.credentials.unlisted
    for name in names:
        if name in unlisted:
            problems.append(
                preflight.Problem(
                    "origin",
                    f"project {run.project} lists no origin for {name}, so no credential is leased for it: add "
                    "the repo with its origin to the harness and run `evo-agents hub project register` again",
                )
            )
    asked = [name for name in names if name not in unlisted and name not in fetched]
    reached = await parallel.each(run, asked, lambda name: reach(run, name, push=name in pushes))
    problems += [found for found in reached if found is not None]
    opens = [
        name
        for name, found in zip(asked, reached, strict=True)
        if found is None and name in pulls and not run.credentials.guarded and on_github(run, name) is not None
    ]
    opened = await parallel.each(run, opens, lambda name: opens_pull(run, name))
    problems += [found for found in opened if found is not None]
    problems += preflight.verify_problems(verify, path)
    problems += preflight.hidden_problems(hidden or [], path)
    problems += await environment_problems(run, names)
    problems += browser_problems(run, path)
    if problems:
        for problem in problems:
            run.note(f"Preflight: {problem.message}.", cause=problem.cause)
        raise RunFailed(cut(preflight.summary(problems), MAX_ERROR_CHARS), cause=problems[0].cause)
    checked = [f"{len(names)} repo(s)"]
    left = sum(1 for name in names if name in fetched)
    if left:
        checked[0] += f" ({left} left to their fetch: the run goes on in their worktrees)"
    if opens:
        checked.append(f"the pull requests of {len(opens)} repo(s) on GitHub")
    if verify or hidden:
        checked.append(f"the programs of {len(verify) + len(hidden or [])} command(s)")
    if run.agent.environment.container:
        checked.append(f"environment {run.agent.environment.name}, its engine and image")
    if run.spec.get("browser") is True:
        checked.append("Chromium and node for its browser")
    run.note(f"Preflight passed: {' and '.join(checked)}.")


def browser_problems(run: Run, path: str | None) -> list[preflight.Problem]:
    """For a run dispatched with browser, Chromium not installed on this worker or node not on ``path``, each with
    cause missing_tool (``browser.install.problems``); none for any other run."""
    if run.spec.get("browser") is not True:
        return []
    return browser_install.problems(run.daemon.home, path)


async def environment_problems(run: Run, names: list[str]) -> list[preflight.Problem]:
    """Why the run's agent cannot run in the environment its claim names, each with cause environment: none for the
    host; for a plugin's environment, what ``Container.preflight`` finds (which may pull its image)."""
    try:
        environment = run.agent.environment
    except ValueError as exc:
        return [preflight.Problem("environment", f"the claim names an environment this worker does not read: {exc}")]
    if not isinstance(environment, Container):
        return []
    checkouts = [run.daemon.checkout_for(run.project, name) for name in names]
    said = await asyncio.to_thread(
        environment.preflight,
        kind=str(run.spec.get("kind") or "step"),
        mode=run.mode,
        runtime=run.runtime,
        core_paths=[run.daemon.home.root, *(path for path in checkouts if path is not None)],
    )
    return [preflight.Problem("environment", text) for text in said]


async def reach(run: Run, name: str, *, push: bool) -> preflight.Problem | None:
    """The problem git has reading the origin of ``name`` (and pushing to it, with ``push``) with what the run holds,
    or None; a repo without a checkout or an origin here is left to the steps after, which say so."""
    checkout = run.daemon.checkout_for(run.project, name)
    urls = run.credentials.origins.get(name) or []
    if checkout is None or not urls or all(preflight.is_local(url) for url in urls):
        return None
    covered = run.credentials.covers(name)
    if push and run.credentials.guarded and not covered:
        who = "a run of the Curator" if run.curator is not None else f"a {run.spec.get('kind') or 'guarded'} run"
        return preflight.Problem(
            "credentials",
            f"{name}: {who} pushes only with the credential the hub leased it, and no lease covers "
            f"{urls[0]}: this machine's own credentials are not used",
        )
    with run.credentials.ticketed(run.daemon.env) as env:
        refused = await preflight.can_reach(checkout, run.id, push=push, env=env)
    if refused is None:
        return None
    what, said = refused
    verb = "read" if what == "read" else "push to"
    if covered:
        why = "with the credential the hub leased the run"
    else:
        why = f"with this machine's own credentials (the hub leased none: {run.credentials.unleased.get(name)})"
        if name not in run.credentials.unleased:
            why = "with this machine's own credentials (no lease of the run covers it)"
    return preflight.Problem("credentials", f"git cannot {verb} {name} at {urls[0]} {why}: {said}")


def on_github(run: Run, name: str) -> tuple[str, str, str] | None:
    """The URL of the origin of ``name``'s checkout here on github.com, with the repo's owner and name there; None for
    an origin anywhere else (GitLab, another host, a path on this machine), whose pull requests are not checked."""
    for url in run.credentials.origins.get(name) or []:
        found = github_repo(url)
        if found is not None:
            return url, *found
    return None


async def opens_pull(run: Run, name: str) -> preflight.Problem | None:
    """The problem the run's agent has opening a pull request in ``name`` on GitHub, or None (``preflight``,
    ``preflight.pull_refusal``). GitHub is asked with each token the agent may open one with, in the order it would
    take them: the one gh takes in its environment (GH_TOKEN, else GITHUB_TOKEN, else this machine's own gh login,
    which an agent with a config dir of its own does not see, ``claude_home.wanted``), then the run's git lease that
    covers the repo's origin (the GitHub App token the hub leased it). The first that can open one passes the check,
    with a note naming those refused before it; an answer that is not clear (``preflight.Unclear``: gh or GitHub not
    answering) passes it too, noted. A problem with cause credentials only when each token was refused, naming each
    and what it lacks, or when the agent holds none."""
    found = on_github(run, name)
    if found is None:
        return None
    url, owner, repo = found
    env = run.agent.env()
    held = run.credentials
    tokens: list[tuple[str, str]] = []  # (what the token is, as a message names it; its value)
    none: list[str] = []  # why the agent holds no token of a kind
    unclear: list[str] = []
    login = not claude_home.wanted(run.spec, run.runtime, held.gateway is not None)
    try:
        value, source = await preflight.gh_token(env, login=login)
    except preflight.Unclear as exc:
        unclear.append(f"this machine's gh: {exc}")
    else:
        if value is None:
            none.append(source)
        elif source == "gh":
            tokens.append(("this machine's own gh login", value))
        elif source in held.agent_vars:
            tokens.append((f"the {source} the hub leased the run", value))
        else:
            tokens.append((f"this machine's {source}", value))
    lease = None
    if name not in held.unleased:
        lease = next(
            filter(None, (credentials.covering(held.leases, item) for item in held.origins.get(name) or [])), None
        )
    if lease is None:
        reason = held.unleased.get(name)
        none.append(
            f"the hub leased the run no credential for it: {reason}" if reason else "no lease of the run covers it"
        )
    elif all(lease.value != value for _, value in tokens):
        if lease.provider == "github-app":
            tokens.append((f"the GitHub App token the hub leased the run ({lease.name})", lease.value))
        else:
            tokens.append((f"the git secret {lease.name} the hub leased the run", lease.value))
    refusals: list[str] = []
    for what, value in tokens:
        try:
            why = await preflight.pull_refusal(owner, repo, value)
        except preflight.Unclear as exc:
            unclear.append(f"{what}: {exc}")
            continue
        if why is None:
            if refusals:
                run.note(f"Preflight: {'; '.join(refusals)}; {what} can open a pull request in {name}.")
            return None
        refusals.append(f"{what} {why}")
    if unclear:
        run.note(
            f"Preflight: whether the run can open a pull request in {name} is not known "
            f"({'; '.join([*refusals, *unclear])}): the agent starts all the same."
        )
        return None
    if refusals:
        said = "; ".join([*refusals, *none])
        return preflight.Problem("credentials", f"the run cannot open a pull request in {name} at {url}: {said}")
    return preflight.Problem(
        "credentials",
        f"the run holds no credential to open a pull request in {name} at {url} with: {'; '.join(none)}",
    )
