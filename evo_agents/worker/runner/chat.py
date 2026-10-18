"""A chat run (kind ``chat``, ``ChatRun``, ``evo_agents.hub.chat_runs``): one turn of an agent mentioned in a group
chat, which answers in the thread and may change code.

The daemon takes the run's leases for the repos of its project it has a checkout of (the others it leaves out, saying
so), checks it can read each, and makes the directory ``~/.evo/worker/worktrees/<project>-<run>`` with a writable
worktree of each, on the run's branch ``chat/<id>`` (the id of the first run of the session), from origin's branch when
the session pushed it already, else from origin's default branch; a run that goes on in the session of an earlier one
adopts that run's directory, worktrees and session instead (``Conversation.adopt``). The agent works in EVO_RUN_KIND
``chat``, its turn's last message going to the hub as its reply in the thread (``conversation.post_last_message``);
the messages people wrote to it in the thread while it worked start another turn in the same session
(``chat_runs.REPLY_PROMPT``). When no message is left, the run ends: the daemon commits what the agent left in each
worktree and pushes the run's branch of each repo whose commits the branch lacks on origin, never a default branch and
never forced (``gitops.check_push``, kind ``chat``), tells the hub (POST /v1/worker/runs/{id}/chat/pulls), which opens
the draft pull request and links it in the thread, and reports ``done``. It never waits: the next turn of the session is
another run. The worktrees stay on their branch for it, until the cleanup of old runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import chat_runs, runs
from evo_agents.worker import gitops
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable
from evo_agents.worker.runner import access, origin, push, verify
from evo_agents.worker.runner.common import HARD_STOP_TRIES, RunFailed, cut, push_cause, wait_or
from evo_agents.worker.runner.conversation import Conversation, post_last_message
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

NOTHING = {"files": 0, "insertions": 0, "deletions": 0}


class ChatRun:
    """A chat run: a writable worktree of each repo of its project this worker has a checkout of, on the run's branch,
    the agent answering in the thread turn after turn while people write to it, and the branch of each repo it changed
    pushed at its end, with the hub told so for the draft pull request."""

    parks = False
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
        self.left_out: list[str] = []  # repos of the run this worker has no checkout of
        self.last_said: str | None = None  # the agent's last reply in the thread

    async def steps(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        resume_of = self.conversation.resume_of
        goes_on = f", going on in the session of run #{resume_of}" if resume_of else ""
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: a chat run of project {run.project} on "
            f"{self.branch}, {run.runtime}, timeout {run.timeout_s // 60} min of agent time{goes_on}."
        )
        cls = run.agent.runtime_class()
        if run.mode == "interactive":
            raise RunFailed("a chat run runs headless only")
        if not chat_runs.is_chat_branch(self.branch):
            raise RunFailed(f"a chat run works on a branch chat/<run>, not {self.branch}")
        if resume_of is not None:  # the earlier run's directory, worktrees and session, when they are still here
            self.conversation.adopt()
        held = self.held_repos(repos)
        names = [entry["repo"] for entry in held]
        await access.take_credentials(run, names)
        run.check()
        await access.check(run, names, path=access.run_path(run))
        run.check()
        await self.prepare(held)
        prompt, session_id = await self.conversation.first_turn(self.prompt())
        await self.conversation.turns(cls, prompt, session_id)
        run.summary = verify.read_summary(run, self.directory.path) or (
            cut(self.last_said, MAX_SUMMARY_CHARS) if self.last_said else None
        )
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        diffstat = await self.push_repos()
        await run.reports.end(
            "done", diffstat=diffstat, summary=run.summary, usage=run.outcome.usage if run.outcome else None
        )

    # The turns

    async def after_turn(self) -> list[dict]:
        """After each turn: the agent's reply in the thread, then the messages people wrote to it meanwhile, which
        start its next turn; none ends the run."""
        run = self.run
        said = await post_last_message(run)
        if said is not None:
            self.last_said = said
        unread = await run.inbox.unread()
        if unread:
            run.note(f"{len(unread)} message(s) came in the thread while the agent worked: a new turn takes them.")
        return unread

    def prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        prompt = self.run.spec.get("prompt") or ""
        moved = self.directory.moved()
        extra = []
        if moved:
            extra += ["", "Worktree folders that are not named as their repo:", *moved]
        if self.left_out:
            extra += ["", f"Repos of the project this worker has no checkout of: {', '.join(self.left_out)}."]
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)

    # The directory and its worktrees

    def held_repos(self, repos: list[dict]) -> list[dict]:
        """The repos of the run this worker has a checkout of; the others are noted, and left out."""
        run = self.run
        held = []
        for entry in repos:
            name = entry.get("repo")
            if isinstance(name, str) and name and run.daemon.checkout_for(run.project, name) is not None:
                held.append(entry)
            else:
                self.left_out.append(str(name))
        if self.left_out:
            run.note(f"No checkout of {', '.join(self.left_out)} on this worker: the agent works without them.")
        return held

    async def prepare(self, held: list[dict]) -> None:
        """The run's directory with a writable worktree of each repo of ``held`` on the run's branch: the earlier
        run's, adopted, else new ones."""
        run, directory = self.run, self.directory
        if directory.path is None:
            directory.claim("mention the agent again")
        directory.enter()
        taken = {workspace.worktree.name for workspace in directory.workspaces.values()}
        for entry in held:
            name = entry["repo"]
            if name in directory.workspaces:
                continue
            folder = gitops.folder_name(name, taken)
            taken.add(folder)
            directory.workspaces[name] = await self.make_worktree(
                name, run.daemon.checkout_for(run.project, name), folder
            )
            directory.save()
        (directory.path / runs.RESULT_DIR).mkdir(mode=0o700, exist_ok=True)
        directory.save()

    async def make_worktree(self, name: str, checkout: Path, folder: str) -> gitops.Workspace:
        """A worktree of ``name`` at ``<directory>/<folder>`` on the run's branch: from origin's branch when the
        session pushed it already, else from origin's default branch; on evo-run/<run>/<folder> when the branch is
        checked out elsewhere or has commits here that the start lacks."""
        run, branch = self.run, self.branch
        path = self.directory.path / folder
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await origin.fetch(run, checkout)
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(run.daemon.config, run.project, name)
            protected = tuple(sorted(origin.protected_branches(remote_head, hub_default)))
            defaults = [f"refs/remotes/origin/{default}" for default in (remote_head, hub_default) if default]
            refs = (f"refs/remotes/origin/{branch}", *defaults, "refs/remotes/origin/HEAD", "HEAD")
            start, base = await origin.start_point(checkout, refs)
            if base is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to start from")
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and mention the agent again")
            side = origin.RUN_REPO_BRANCH.format(id=run.id, folder=folder)
            local_branch, why = await origin.side_or_branch(checkout, branch, base, side)
            if why is not None:
                run.note(f"{branch} of {name} {why}; the run works on {local_branch} and pushes it to {branch}.")
            try:
                await gitops.add_worktree(checkout, path, local_branch, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        run.note(f"Worktree {path} of {name} on {local_branch} at {base[:12]} ({start}).", worktree=str(path))
        return gitops.Workspace(
            repo=name,
            branch=branch,
            plan_branch=None,
            checkout=checkout,
            worktree=path,
            local_branch=local_branch,
            base=base,
            protected=protected,
        )

    # The end

    async def push_repos(self) -> dict:
        """Commit what the agent left in each repo and push the run's branch of each repo it changed; the diffstat of
        the whole run."""
        total = dict(NOTHING)
        for name, workspace in self.directory.workspaces.items():
            stat = await self.push_repo(name, workspace)
            for key in total:
                total[key] += stat[key]
        return total

    async def push_repo(self, name: str, workspace: gitops.Workspace) -> dict:
        """Commit what the agent left in the worktree of ``name`` and push the run's branch when it has commits the
        worktree did not start with; the repo's diffstat. The hub hears of each push that moved the branch."""
        run, path = self.run, workspace.worktree
        if not path.is_dir():
            raise RunFailed(f"the worktree of {name} at {path} is gone")
        try:
            await push.commit(run, path, name)
        except gitops.GitError as exc:
            raise RunFailed(f"committing what the agent left in {name} failed: {exc}") from None
        if await gitops.rev(path, "HEAD") == workspace.base:
            return dict(NOTHING)  # the agent changed nothing in this repo
        head_branch = await gitops.current_branch(path)
        if head_branch != workspace.local_branch:
            raise RunFailed(
                f"the worktree of {name} is on "
                f"{gitops.branch_ref(head_branch) if head_branch else 'a detached HEAD'}, not "
                f"{gitops.branch_ref(workspace.local_branch)}: the agent switched branches, and the worker does not "
                "push it",
                cause="branch_mismatch",
            )
        run.check()

        async def pushed_branch() -> gitops.Pushed:
            with run.credentials.ticketed(run.daemon.env) as env:
                return await gitops.push(
                    path, workspace.branch, protected=workspace.protected, kind="chat", plan_branch=None, env=env
                )

        try:
            pushed = await run.credentials.with_renewal(name, pushed_branch)
        except gitops.PushRefused as exc:
            raise RunFailed(f"{name}: {exc}") from None
        except gitops.GitError as exc:
            raise RunFailed(f"git push of {name} to {workspace.branch} failed: {exc}", cause=push_cause(exc)) from None
        if pushed.changed:
            run.note(
                f"Pushed {pushed.head[:12]} of {name} to {workspace.branch} on origin "
                f"({len(pushed.commits)} commit(s)).",
                commit_sha=pushed.head,
                repo=name,
            )
            await self.tell_hub(name, workspace.branch, pushed.head)
        return await gitops.diffstat(path, workspace.base)

    async def tell_hub(self, name: str, branch: str, head: str) -> None:
        """Tell the hub the run pushed ``branch`` of ``name``, for its draft pull request, trying a few times; what
        the hub does not take is noted, and the run goes on."""
        run = self.run
        backoff = Backoff()
        for _ in range(HARD_STOP_TRIES + 1):
            try:
                answer = await run.daemon.hub.in_chats(
                    run.id, "chat/pulls", {"repo": name, "branch": branch, "head": head}
                )
                run.note(f"The hub linked {branch} of {name} in the thread: {answer.get('url') or 'no pull request'}.")
                return
            except Unreachable:
                await wait_or(run.daemon.hard_stop, backoff.next())
            except HubProblem as exc:
                run.note(f"The hub did not take the push of {branch} of {name}: {exc}")
                return
        run.note(f"The hub did not answer: the push of {branch} of {name} was not linked in the thread.")

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        return await self.directory.diff()

    async def release_branch(self) -> None:
        """The worktrees stay on the run's branch: the next run of the session goes on in them."""
