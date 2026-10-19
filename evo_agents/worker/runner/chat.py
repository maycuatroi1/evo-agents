"""A chat run (kind ``chat``, ``ChatRun``, ``evo_agents.hub.chat_runs``): one turn of an agent mentioned in a group
chat, which answers in the thread and may change code.

The daemon takes the run's leases for the repos of its project it has a checkout of (the others it leaves out, saying
so), checks it can read each, and makes the directory ``~/.evo/worker/worktrees/<project>-<run>`` with a writable
worktree of each, on the run's branch ``chat/<id>`` (the id of the first run of the session), from origin's branch when
the session pushed it already, else from origin's default branch; a run that goes on in the session of an earlier one
adopts that run's directory, worktrees and session instead (``Conversation.adopt``). Such a run takes leases of its own
too, but asks no origin before its agent starts: the fetch of each worktree's checkout it adopted, several at a time,
is the check that git reads it (``BranchWork.fetch_adopted``), and only the push at its end asks git to push; a repo
whose worktree is no longer here gets a new one, its origin asked first as on the first turn. A push origin refuses
fails the run, whose error the hub says in the thread, and leaves the commit on the run's branch in the worktree, for
the run that goes on in it to push; one origin declined by a rule of its own (a hook, a protected branch, a ruleset)
fails it with cause push_declined, after which the hub goes on in the session at the next message of the thread, as
after a run done (``chat_runs.goes_on``). The agent works in EVO_RUN_KIND
``chat``, its turn's last message going to the hub as its reply in the thread (``conversation.post_last_message``);
the messages people wrote to it in the thread while it worked start another turn in the same session
(``chat_runs.REPLY_PROMPT``). When no message is left, the run ends: the daemon commits what the agent left in each
worktree and pushes the run's branch of each repo whose commits the branch lacks on origin, never a default branch and
never forced (``gitops.check_push``, kind ``chat``), tells the hub (POST /v1/worker/runs/{id}/chat/pulls), which opens
the draft pull request and links it in the thread, and reports ``done``. Its agent may ask the owner decisions
(``evo-agents worker ask``, category ``triage``); a turn that ends with one open leaves the run ``waiting`` for the
answers or a message in the thread, as an author run does, and a run the hub parks keeps its session and worktrees for
the run that resumes it. A run that asked none ends with its turn: the next turn of the session is another run. The
worktrees stay on their branch for it, until the cleanup of old runs.

The daemon's notes of the steps of the run's setup name the step each starts (``chat_runs.SETUP_FIELD``: claimed,
checking, opening with the repo, starting, and working once a repo the agent asked for is open), which the thread
shows under the message the run took (``evo_agents.hub.server.chat_progress``).

A daemon of ``runs.LAZY_CHAT_REPOS`` gets the first run of a session with the project's harness alone (or no repo),
and its agent opens each other repo it needs with ``evo-agents worker repo open REPO`` (``chat_runs.OPEN_COMMAND``),
which asks the daemon on the run's socket (``credentials.ask_open``). The daemon does the rest, never the agent
(``ChatRun.open_for_agent``): it asks the hub to add the repo to the run (POST /v1/worker/runs/{id}/chat/repos), takes
the run's leases again so they cover it (``RunCredentials.add``), fetches its checkout with them and makes its
worktree on the run's branch in the run's directory, as the first turn makes the others (``BranchWork.make_worktree``),
notes it in the run's events and record, and answers the worktree's path, which the command prints. A repo open
already answers its path again, without the hub. The daemon refuses a repo this worker has no checkout of before it
asks the hub, the hub one the project does not list, and the command prints one ``error:`` line and exits 1. The agent
opens repos only while its turns run. The push at the run's end takes the run's first repos and those opened, no
other; the run that goes on in the session gets them all from the hub and adopts their worktrees, fetched as on any
next turn.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import chat_runs, runs
from evo_agents.worker import gitops
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner import access, transitions, verify
from evo_agents.worker.runner.branch_work import CHAT, BranchWork
from evo_agents.worker.runner.common import HARD_STOP_TRIES, Parked, RunFailed, Stopped, cut, wait_or
from evo_agents.worker.runner.conversation import Conversation, post_last_message
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class ChatRun:
    """A chat run: a writable worktree of each repo of its project this worker has a checkout of, on the run's branch,
    the agent answering in the thread turn after turn while people write to it, and the branch of each repo it changed
    pushed at its end, with the hub told so for the draft pull request."""

    parks = True
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"chat run of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(
            run,
            self.directory,
            reply_prompt=chat_runs.REPLY_PROMPT,
            resume_prompt=chat_runs.RESUME_PROMPT,
            after_turn=self.after_turn,
        )
        self.branch = str(run.spec.get("branch") or chat_runs.branch_of(run.id))
        self.work = BranchWork(run, self.directory, self.branch, CHAT)  # its worktrees and their push
        self.last_said: str | None = None  # the agent's last reply in the thread
        self.opening = asyncio.Lock()  # one repo opened at a time, and none while the turns end
        self.opens = False  # the agent may open repos: from its first turn to the end of its last
        run.credentials.opener = self.open_for_agent  # `evo-agents worker repo open` asks on the run's socket

    async def steps(self) -> None:
        try:
            await self._work()
        except Parked:
            await self.conversation.park()

    async def _work(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        resume_of = self.conversation.resume_of
        goes_on = f", going on in the session of run #{resume_of}" if resume_of else ""
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: a chat run of project {run.project} on "
            f"{self.branch}, {run.runtime}, timeout {run.timeout_s // 60} min of agent time{goes_on}.",
            setup="claimed",
        )
        cls = run.agent.runtime_class()
        if run.mode == "interactive":
            raise RunFailed("a chat run runs headless only")
        if not chat_runs.is_chat_branch(self.branch):
            raise RunFailed(f"a chat run works on a branch chat/<run>, not {self.branch}")
        if resume_of is not None:  # the earlier run's directory, worktrees and session, when they are still here
            self.conversation.adopt()
        held = self.work.held_repos(repos)
        names = [entry["repo"] for entry in held]
        checked = f"{len(names)} repo(s): {', '.join(names)}" if names else "the run has no repo"
        run.note(f"Checking access of the run: {checked}.", setup="checking")
        await access.take_credentials(run, names)  # the leases are the run's own, a run going on in a session too
        run.check()
        await access.check(run, names, path=access.run_path(run), fetched=self.work.adopted(names))
        run.check()
        await self.work.prepare(held)  # its notes of each repo's fetch say it opens that repo (BranchWork.setup)
        run.note(f"The run's worktrees are ready: starting {run.runtime}.", setup="starting")
        self.opens = True
        try:
            prompt, session_id = await self.conversation.first_turn(self.prompt())
            await self.conversation.turns(cls, prompt, session_id)
        finally:
            self.opens = False  # no repo opens once the turns are over
        async with self.opening:  # one being opened is made before the push takes the worktrees
            pass
        run.summary = verify.read_summary(run, self.directory.path) or (
            cut(self.last_said, MAX_SUMMARY_CHARS) if self.last_said else None
        )
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        usage = run.outcome.usage if run.outcome else None
        try:
            diffstat = await self.work.push_repos()
        except RunFailed as exc:  # its turns cost what they cost: the chat's budget and the next run count it
            exc.fields.setdefault("usage", usage)
            raise
        await run.reports.end("done", diffstat=diffstat, summary=run.summary, usage=usage)

    # The turns

    async def after_turn(self) -> list[dict]:
        """After each turn: the agent's reply in the thread, then what starts its next turn: the messages people wrote to
        it meanwhile (``transitions.author_wakes``: one of the thread, or the answers to every decision the agent
        asked that is open); else, while a decision is open, the run waits for them; none ends the run."""
        run = self.run
        said = await post_last_message(run)
        if said is not None:
            self.last_said = said
        asked = self.conversation.open_asked()
        unread = await run.inbox.unread()
        if transitions.author_wakes(unread, asked):
            run.note(f"{len(unread)} message(s) came in the thread while the agent worked: a new turn takes them.")
            return unread
        return await self.conversation.wait_for_owner(asked, lambda found: transitions.author_wakes(found, asked))

    def prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        prompt = self.run.spec.get("prompt") or ""
        extra = self.work.where()
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)

    # The repos its agent opens

    async def open_for_agent(self, name: str) -> dict:
        """``evo-agents worker repo open NAME`` of the run's agent, asked on the run's socket: ``{repo, path, opened}``
        with the path of the repo's worktree, the one it has or one made now (see the module's docstring), or
        ``{error}`` with why not. One at a time."""
        run = self.run
        async with self.opening:
            workspace = self.directory.workspaces.get(name)
            if workspace is not None:
                return {"repo": name, "path": str(workspace.worktree), "opened": False}
            run.note(f"The agent asks to open {name}.", repo=name, setup="opening")
            refusal = await self._open(name)
            if refusal is not None:
                note = f"The agent asked to open {name}, which the run did not: {refusal}."
                run.note(note, repo=name, setup="working")
                return {"error": refusal}
            workspace = self.directory.workspaces[name]
            run.note(
                f"The agent opened {name}: its worktree is {workspace.worktree}, on {workspace.local_branch}.",
                repo=name,
                worktree=str(workspace.worktree),
                setup="working",
            )
            return {"repo": name, "path": str(workspace.worktree), "opened": True}

    async def _open(self, name: str) -> str | None:
        """Add repo ``name`` to the run on the hub, take the leases that cover it and make its worktree; why not, or
        None once it is made."""
        run = self.run
        if not self.opens or self.directory.path is None:
            return f"run {run.id} opens a repo only while its agent's turns run"
        checkout = run.daemon.checkout_for(run.project, name)
        if checkout is None:
            return (
                f"worker {run.daemon.config.name} has no checkout of {name} of project {run.project}: the run cannot "
                "open it here"
            )
        refusal = await self._add_on_hub(name)
        if refusal is not None:
            return refusal
        urls: list[str] = []
        with contextlib.suppress(gitops.GitError, OSError):
            urls = await gitops.remote_urls(checkout)
        await run.credentials.add(name, urls)
        try:
            await self.directory.make_each([name], lambda repo, folder: self.work.make_worktree(repo, checkout, folder))
        except RunFailed as exc:
            return exc.error
        except Stopped as exc:
            return f"the run is stopping ({exc.reason})"
        except (gitops.GitError, OSError) as exc:
            return f"making the worktree of {name} failed: {exc}"
        return None

    async def _add_on_hub(self, name: str) -> str | None:
        """Ask the hub to add repo ``name`` to the run (POST /v1/worker/runs/{id}/chat/repos), a few times while it
        does not answer; its refusal, or None once it took it."""
        run = self.run
        backoff = Backoff()
        for attempt in range(HARD_STOP_TRIES + 1):
            try:
                await run.daemon.hub.in_chats(run.id, "chat/repos", {"repo": name})
                return None
            except Unreachable as exc:
                if attempt == HARD_STOP_TRIES or await wait_or(run.daemon.hard_stop, backoff.next()):
                    return f"the hub did not answer ({exc}): ask again"
            except HubProblem as exc:
                return str(exc)
        return "the hub did not answer: ask again"

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        return await self.directory.diff()

    async def release_branch(self) -> None:
        """The worktrees stay on the run's branch: the next run of the session goes on in them."""
