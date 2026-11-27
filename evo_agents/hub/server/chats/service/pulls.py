"""The pull request of a chat run's branch, and the repos a chat run's agent opens (``chats.service.agents`` has the
run and its agent).

- POST /v1/worker/runs/{id}/chat/pulls: the worker pushed the run's branch of a repo (chat runs only): the hub opens a
  draft pull request of it on GitHub with the workers' App, or finds the one open, titled as the session (its first
  run), and the agent's message in the thread links it; a repo elsewhere, or a hub without the App, gets the message
  without one.
- POST .../chat/repos: the worker opened a repo of the project in the chat run, as its agent asked (``evo-agents
  worker repo open``, a daemon of ``runs.LAZY_CHAT_REPOS``; chat runs only): the hub adds it to the run's repos, on
  the run's branch, so the run's leases (``credentials``) and ``chat/pulls`` take it, and the runs after it in the
  session start with it. Only a repo of the project the worker has a checkout of (422 for a repo the project does not
  list, 409 for one the worker has no checkout of, or past ``runs.MAX_RUN_REPOS``); one the run works in already is
  answered as it is (``added`` false).

A repo opened is audited as run.open_repo, as the worker, in the run's project.
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import guard, runs
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.chats.models.agents import ChatPull, ChatPushed, ChatRepo, ChatRepoIn
from evo_agents.hub.server.chats.models.runs import ChatRun
from evo_agents.hub.server.chats.repository import agents as store
from evo_agents.hub.server.chats.repository.runs import project_repos
from evo_agents.hub.server.chats.service.agents import AgentRun, agent_run
from evo_agents.hub.server.chats.service.runs import agent_access, chat_run, post_agent
from evo_agents.hub.server.forge.models.github import GitHubRefused, GitHubUnavailable
from evo_agents.hub.server.merge.repository import draft_pulls as draft_rows
from evo_agents.hub.server.merge.service import draft_pulls, trailers
from evo_agents.hub.server.repo_origins import repo_origins
from evo_agents.hub.server.runs.models import RunRepo
from evo_agents.hub.server.runs.service.claims import worker_of
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# The pull request of a chat run's branch


async def _pushed_run(
    conn: AsyncConnection, user: Principal, run_id: int, body: ChatPushed
) -> tuple[AgentRun, ChatRun, str | None, str | None]:
    """(the run, the chat run, the repo's origin and default branch) of a push the worker reports; 404 or 422 when it
    is not the run's."""
    run = await agent_run(conn, user, run_id)
    found = await chat_run(conn, run_id) if run.kind == "chat" else None
    if found is None:
        raise HTTPException(404, f"run {run_id} is a {run.kind} run: only a chat run's branch gets a pull request here")
    if body.branch != run.branch or not rules.is_chat_branch(body.branch):
        raise HTTPException(422, f"run {run_id} pushes {run.branch}, not {body.branch}")
    if body.repo not in [entry.get("repo") for entry in run.repos if isinstance(entry, dict)]:
        raise HTTPException(422, f"run {run_id} does not work in {body.repo}")
    row = await store.repo_row(conn, run.project_id, body.repo)
    return run, found, (row.origin if row else None), (row.default_branch if row else None)


async def pushed(engine: AsyncEngine, app, user: Principal, run_id: int, body: ChatPushed) -> ChatPull:
    """The worker pushed the chat run's branch of a repo: its draft pull request into the branch it was made from
    (``draft_pulls.open_for``) with the workers' GitHub App ``app`` (None: none), kept on the run and linked in the
    thread; none for a branch with nothing to merge."""
    async with engine.begin() as conn:
        run, found, origin, default = await _pushed_run(conn, user, run_id, body)
        # the session's title: its first run's, which the branch names
        first = rules.session_of(body.branch) or run_id
        title = await store.run_title(conn, first) or body.branch
        signed = await trailers.block(conn, run_id)
        policy = await guard.policy(conn)
    target = github_repo(origin) if origin else None
    url, number, opened, problem, draft, made = None, None, False, None, True, None
    if target is not None and app is not None:
        # The handle in code spans: a bare @handle on GitHub would mention the GitHub user of that login
        text = (
            f"Opened by the hub for `@{run.profile.handle}`, from chat {found.chat_id} (run #{run_id}). Not merged."
            + signed
        )
        try:
            title, text = guard.scrub(policy, f"github.com/{target[0]}/{target[1]}", title[:240], text)
            made = await draft_pulls.open_for(
                app, target, body.branch, body.head, title, text, base=body.base, defaults=(default,)
            )
            pull_made = made.pull or {}
            url, number, opened = pull_made.get("html_url"), pull_made.get("number"), made.opened
            draft = pull_made.get("draft") is not False
        except (GitHubRefused, GitHubUnavailable) as exc:
            problem = str(exc)
            log.warning("chat run pull request not opened", extra={"run_id": run_id, "repo": body.repo, "why": problem})
    elif target is not None:
        url = f"https://github.com/{target[0]}/{target[1]}/tree/{body.branch}"
    pull = number is not None
    if made is not None and made.nothing is not None:
        said = rules.nothing_text(body.repo, body.branch, made.nothing, (made.closed or {}).get("html_url"))
    else:
        said = rules.pull_text(body.repo, body.branch, url, pull=pull, opened=opened, problem=problem, draft=draft)
    async with engine.begin() as conn:
        entry = draft_pulls.kept(body.repo, body.branch, body.head, made) if made is not None else None
        if entry is not None:
            await draft_rows.keep(conn, run_id, entry)
        if made is not None and made.closed is not None and type(made.closed.get("number")) is int:
            await draft_rows.mark(conn, run.project_id, body.repo, made.closed["number"], state="closed")
        chat = await agent_access(conn, found.chat_id, run.profile, run.principal.user_id)
        message_id = 0
        if chat is not None:
            message_id, _ = await post_agent(conn, chat, run.profile, run_id, said, found.root_id)
    return ChatPull(repo=body.repo, branch=body.branch, url=url, number=number, opened=opened, message_id=message_id)


# The repos a chat run's agent opens


async def _refuse_checkout(conn: AsyncConnection, run: AgentRun, worker, name: str) -> None:
    """409 when the worker has no checkout of repo ``name`` of the run's project and cannot clone it."""
    project = run.profile.project
    if f"{project}/{name}" in (worker.checkouts or {}):
        return
    # a daemon that clones what it lacks clones it first, from the origin a project lists for it
    if runs.CLONES_CHECKOUTS not in (worker.features or ()):
        why = "the run cannot open it here"
    elif not await repo_origins(conn, run.project_id, run.principal.user_id, [name]):
        why = "and no project of the run's owner lists an origin to clone it from"
    else:
        why = None
    if why is not None:
        raise HTTPException(409, f"worker {worker.name} has no checkout of {name} of project {project}: {why}")


async def open_repo(engine: AsyncEngine, user: Principal, run_id: int, body: ChatRepoIn) -> ChatRepo:
    """The worker opened repo ``repo`` of the project in the chat run, as its agent asked: the hub adds it to the run's
    repos on the run's branch (see the module's docstring)."""
    async with UnitOfWork(engine) as conn:
        run = await agent_run(conn, user, run_id)
        if run.kind != "chat":
            raise HTTPException(404, f"run {run_id} is a {run.kind} run: only a chat run's agent opens a repo here")
        project, name = run.profile.project, body.repo
        names = await project_repos(conn, run.project_id)
        if name not in names:
            listed = ", ".join(names) or "none"
            raise HTTPException(422, f"project {project} has no repo {name}: its repos are {listed}")
        await _refuse_checkout(conn, run, await worker_of(conn, user), name)
        repos = [dict(entry) for entry in run.repos if isinstance(entry, dict)]
        added = name not in [entry.get("repo") for entry in repos]
        if added:
            if len(repos) >= runs.MAX_RUN_REPOS:
                raise HTTPException(409, f"run {run_id} works in {len(repos)} repos, the most a run works in")
            repos.append({"repo": name, "branch": run.branch})
            await store.set_repos(conn, run_id, repos)
            await audit.record(
                conn,
                actor_id=user.user_id,
                token_id=user.token_id,
                action=audit.RUN_OPEN_REPO,
                target=f"{project}/chat run:{run_id} repo={name}",
                project_id=run.project_id,
            )
        # a chat run has its branch; cast, so a run without one still fails as before, when ChatRepo validates it
        branch = cast(str, run.branch)
        return ChatRepo(repo=name, branch=branch, added=added, repos=[RunRepo(**entry) for entry in repos])
