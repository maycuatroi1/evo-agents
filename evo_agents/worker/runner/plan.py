"""A plan run (kind ``plan``, ``PlanRun``): every step of a plan not done yet, in one session.

1. ``leased``: the daemon takes the run's leases for all its repos, then makes the directory
   ``~/.evo/worker/worktrees/<project>-<run>`` with a worktree of each repo of the run in it (``directory``), each on
   the branch the plan names for that repo (``evo-run/<run>/<repo>`` when that branch is checked out elsewhere; the
   push still goes to the plan's branch), and writes the plan as claimed to ``.evo-run/plan.yaml`` there. A repo the
   plan names no branch for fails the run before the agent starts, and so does a default branch the plan does not
   name for the repo.
2. ``running``: the agent works in that directory, with EVO_RUN_ID, EVO_RUN_KIND and EVO_WORKER_HOME, and reports
   each step itself through ``evo-agents worker step`` (which runs the step's verify commands again, commits, pushes
   the repo's branch and sends the step report), asks its owner through ``evo-agents worker ask``, and notifies with
   ``evo-agents worker notify``. A default branch the remote moved on gets the step's commits on top of its tip, and
   the step's verify runs again there before the push (``gitops.Replay``); when they do not go on top, or that verify
   fails, ``worker step`` pushes them to ``evo-run/<run>``, sends the owner the notice ``run_failed`` naming it, reports
   nothing and leaves ``runs/<run>/push_conflict.json``, and the run fails with push_conflict once the turn ends.
3. ``waiting``: a turn that ends with a decision of the run open moves the run to waiting (``conversation``); a run
   the hub parks keeps its session and worktrees for the run that resumes it.
4. Once a turn ends with nothing to wait for, the daemon reports ``verifying`` and commits (leaving out what a run
   of one step leaves out) and pushes what each repo has left, never forced (a default branch only when the plan
   names it, with the notice ``push_default_branch``), and reports ``done`` with the agent's summary from
   ``.evo-run/result.json``: no verify commands run at the end, since ``evo-agents worker step`` ran each step's. A
   default branch the remote moved on gets the run's commits on top of its tip (``gitops.Replay``); when they do not go
   on top, they go to ``evo-run/<run>``, the owner gets the notice ``run_failed`` naming it, and the run fails with
   push_conflict. The log and the diffs of every repo are uploaded as for a run of one step.

A Builder of the Curator (``spec["curator"]["role"] == "builder"``) is a plan run whose branch ``curator/...`` is
pushed by the daemon alone, never a default branch (``gitops.check_push``, kind ``curator``), only with a leased
credential, and on GitLab with the push options that open a merge request into the default branch; when its agent
reports a step done, ``evo-agents worker step`` asks the daemon to push (``PlanRun.push_for_agent``).
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from evo_agents.hub import judge, runs
from evo_agents.hub.credentials import normalize_origin
from evo_agents.worker import gitops
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner import access, origin, push, verify
from evo_agents.worker.runner.common import Parked, RunFailed, log, push_cause
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.transitions import start_refs

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class PlanRun:
    """A plan run: a worktree of each of its repos in one directory, the agent in that directory doing every step not
    done yet, waiting for its owner's answers between its turns, and the repos pushed at its end."""

    parks = True
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"plan {run.spec.get('plan_id')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory)
        self.targets: dict[str, str] = {}  # a Builder's repo -> the default branch its merge request goes into
        self.push_lock = asyncio.Lock()  # one push of the run at a time, the daemon's own or one its agent asked for
        if self.builder:  # its agent pushes nothing itself: `evo-agents worker step` asks the daemon
            run.credentials.pusher = self.push_for_agent

    @property
    def builder(self) -> bool:
        """Whether this plan run is a Builder of the Curator, which pushes its branch curator/... alone."""
        return self.run.curator is not None and self.run.curator.get("role") == "builder"

    async def steps(self) -> None:
        try:
            await self._work()
        except Parked:
            await self.conversation.park()

    async def _work(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        names = ", ".join(str(entry.get("repo")) for entry in repos) or "no repo"
        resume_of = self.conversation.resume_of
        resumes = f", going on from parked run #{resume_of}" if resume_of else ""
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: plan {spec.get('plan_id')} over {names}, "
            f"{run.runtime}, {run.mode}, timeout {run.timeout_s // 60} min of agent time{resumes}."
        )
        cls = run.agent.runtime_class()
        run.agent.check_interactive(cls)
        if not repos:
            raise RunFailed("the plan run names no repo to work in")
        names = [entry.get("repo") for entry in repos]
        await access.take_credentials(run, names)
        run.check()
        verify_commands = runs.open_verify(self.plan_body())
        await access.check(run, names, path=access.run_path(run), pushes=names, verify=verify_commands)
        run.check()
        await self.prepare(repos)
        run.check()
        prompt, session_id = await self.conversation.first_turn(self.prompt())
        await self.conversation.turns(cls, prompt, session_id)
        run.summary = verify.read_summary(run, self.directory.path)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        diffstat = await self.push_repos()
        await run.reports.end(
            "done", diffstat=diffstat, summary=run.summary, usage=run.outcome.usage if run.outcome else None
        )

    # The directory and its worktrees

    def plan_body(self) -> dict:
        plan = self.run.spec.get("plan")
        body = plan.get("body") if isinstance(plan, dict) else None
        return body if isinstance(body, dict) else {}

    async def prepare(self, repos: list[dict]) -> None:
        """The run's directory with a worktree of each repo: the parked run's when this run resumes one and they are
        still here, else new ones; and the plan as claimed in .evo-run/plan.yaml."""
        run, directory = self.run, self.directory
        if self.conversation.resume_of is not None:
            self.conversation.adopt()
        if directory.path is None:
            directory.claim("run the plan again")
        directory.enter()
        named = gitops.plan_branches(self.plan_body())
        for name, workspace in list(directory.workspaces.items()):  # a default branch is pushed while the plan names it
            directory.workspaces[name] = dataclasses.replace(workspace, plan_branch=named.get(name))
        taken = {workspace.worktree.name for workspace in directory.workspaces.values()}
        for entry in repos:
            name = entry.get("repo")
            if not isinstance(name, str) or not name:
                raise RunFailed("the plan run names a repo without a name")
            if name in directory.workspaces:
                continue
            branch = entry.get("branch")
            if not isinstance(branch, str) or not branch:
                raise RunFailed(
                    f"the plan names no branch for {name}: a plan run works only on the branch the plan names for "
                    "each repo; add it to the plan's repos"
                )
            checkout = run.daemon.checkout_for(run.project, name)
            if checkout is None:
                raise RunFailed(f"this worker has no checkout of {run.project}/{name}")
            folder = gitops.folder_name(name, taken)
            taken.add(folder)
            directory.workspaces[name] = await self.make_worktree(name, branch, named.get(name), checkout, folder)
            directory.save()
        self.write_plan()
        directory.save()

    async def make_worktree(
        self, name: str, branch: str, plan_branch: str | None, checkout: Path, folder: str
    ) -> gitops.Workspace:
        """A worktree of the checkout of ``name`` at ``<directory>/<folder>`` on the plan's branch for it, from
        origin's branch when there is one (``origin``), or on evo-run/<run>/<folder> when that branch is checked out
        elsewhere or has commits here that the start lacks."""
        run = self.run
        path = self.directory.path / folder
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from and push to")
            await origin.fetch(run, checkout)
            if not await gitops.check_branch_name(checkout, branch):
                raise RunFailed(f"the plan's branch {branch!r} for {name} is not a valid branch name")
            remote_head = await gitops.remote_default_branch(checkout)
            hub_default = default_branch_of(run.daemon.config, run.project, name)
            protected = tuple(sorted(origin.protected_branches(remote_head, hub_default)))
            kind = "curator" if self.builder else "plan"
            try:
                default = gitops.check_push(branch, protected, kind=kind, plan_branch=plan_branch)
            except gitops.PushRefused as exc:
                raise RunFailed(f"the run's branch for {name}: {exc}") from None
            if self.builder:
                self.targets[name] = remote_head or hub_default or "main"
                run.record.setdefault("curator", {})["targets"] = dict(self.targets)
            start, base = await origin.start_point(checkout, start_refs(branch))
            if base is None:
                raise RunFailed(f"the checkout at {checkout} has no commit to start from")
            if path.exists():
                raise RunFailed(f"{path} exists already; remove it and run the plan again")
            side = origin.RUN_REPO_BRANCH.format(id=run.id, folder=folder)
            local_branch, why = await origin.side_or_branch(checkout, branch, base, side)
            if why is not None:
                run.note(f"{branch} of {name} {why}; the run works on {local_branch} and pushes it to {branch}.")
            try:
                await gitops.add_worktree(checkout, path, local_branch, base, reset=True)
            except gitops.GitError as exc:
                raise RunFailed(f"cannot make the worktree {path}: {exc}") from None
        pushes = f"; pushes go to {branch}" + (", a default branch the plan names for it" if default else "")
        run.note(f"Worktree {path} of {name} on {local_branch} at {base[:12]} ({start}){pushes}.", worktree=str(path))
        return gitops.Workspace(
            repo=name,
            branch=branch,
            plan_branch=plan_branch,
            checkout=checkout,
            worktree=path,
            local_branch=local_branch,
            base=base,
            protected=protected,
        )

    def write_plan(self) -> None:
        run = self.run
        plan = run.spec.get("plan") if isinstance(run.spec.get("plan"), dict) else {}
        path = self.directory.path / runs.PLAN_FILE
        path.parent.mkdir(mode=0o700, exist_ok=True)
        header = (
            f"# The plan {run.spec.get('plan_id')} of project {run.project} at revision {plan.get('revision')}, as "
            f"run #{run.id} was claimed. `evo-agents worker plan` prints it as the hub holds it now.\n"
        )
        text = yaml.safe_dump(self.plan_body(), allow_unicode=True, sort_keys=False, width=120)
        path.write_text(header + text, encoding="utf-8")

    def prompt(self) -> str:
        """The run's prompt; built again here when a worktree's folder is not named as its repo."""
        folders = {name: workspace.worktree.name for name, workspace in self.directory.workspaces.items()}
        if all(name == folder for name, folder in folders.items()) and self.run.spec.get("prompt"):
            return self.run.spec["prompt"]
        return runs.build_plan_prompt(self.plan_body(), self.run.spec.get("repos") or [], folders)

    # The end

    async def named_branches(self) -> dict[str, str | None]:
        """repo -> the branch the plan names for it as the hub holds it now, so a default branch is pushed only while
        the plan still names it; as the plan was claimed when the hub does not say."""
        run = self.run
        try:
            view = await run.daemon.hub.plan(run.id)
        except HubProblem as exc:
            log.warning("the plan was not read again before the push", extra={"run_id": run.id, "error": str(exc)})
            view = None
        body = view.get("body") if isinstance(view, dict) else None
        if isinstance(body, dict) and "repos" in body:
            return gitops.plan_branches(body)
        return {name: workspace.plan_branch for name, workspace in self.directory.workspaces.items()}

    async def push_repos(self) -> dict:
        """Commit what the agent left in each repo, and push each repo's branch whose origin lacks its commits; the
        diffstat of the whole run."""
        total = {"files": 0, "insertions": 0, "deletions": 0}
        named = await self.named_branches()
        for name, workspace in self.directory.workspaces.items():
            stat = await self.push_repo(name, workspace, named.get(name))
            for key in total:
                total[key] += stat[key]
        return total

    async def push_repo(self, name: str, workspace: gitops.Workspace, plan_branch: str | None) -> dict:
        """Commit what the agent left in the worktree of ``name`` and push its branch; the repo's diffstat."""
        run = self.run
        path = workspace.worktree
        if not path.is_dir():
            raise RunFailed(f"the worktree of {name} at {path} is gone")
        try:
            await push.commit(run, path, name)
        except gitops.GitError as exc:
            raise RunFailed(f"committing what the agent left in {name} failed: {exc}") from None
        head_branch = await gitops.current_branch(path)
        if head_branch is None:
            raise RunFailed(f"HEAD is detached in the worktree of {name}: the worker does not push a detached HEAD")
        if head_branch != workspace.local_branch:
            raise RunFailed(
                f"the worktree of {name} is on {head_branch}, not {workspace.local_branch}: the agent switched "
                "branches, and the worker does not push it"
            )
        run.check()
        options = self.push_options(name)
        if self.builder and self.forge_origin(name) and not run.credentials.covers(name):
            raise RunFailed(
                f"{name}: a run of the Curator pushes only with the credential the hub leased it, and no lease "
                "covers its origin: this machine's own credentials are not used"
            )
        replay = self.replay(name, workspace)

        async def pushed_branch() -> gitops.Pushed:
            with run.credentials.ticketed(run.daemon.env) as env:
                return await gitops.push(
                    path,
                    workspace.branch,
                    protected=workspace.protected,
                    kind="curator" if self.builder else "plan",
                    plan_branch=plan_branch,
                    env=env,
                    options=options,
                    replay=replay,
                )

        try:
            async with self.push_lock:
                pushed = await run.credentials.with_renewal(name, pushed_branch)
        except gitops.PushRefused as exc:
            raise RunFailed(f"{name}: {exc}") from None
        except gitops.PushConflict as exc:
            run.note(
                f"The commits of {name} did not go on top of {exc.branch} on origin: {exc.side_branch} on origin "
                f"has them, at {exc.head[:12]}.",
                repo=name,
                side_branch=exc.side_branch,
                head=exc.head,
            )
            await push.send_notice(run, gitops.conflict_notice(run.id, name, exc), f"{exc.side_branch} of {name}")
            raise RunFailed(f"{name}: {exc}", cause="push_conflict") from None
        except gitops.GitError as exc:
            raise RunFailed(
                f"git push of {name} to {workspace.branch} on origin failed: {exc}", cause=push_cause(exc)
            ) from None
        stat = await gitops.diffstat(path, workspace.base)
        await self.note_push(name, workspace, pushed)
        return stat

    async def note_push(self, name: str, workspace: gitops.Workspace, pushed: gitops.Pushed) -> None:
        """Say in the run's log what the push of ``name`` did, and tell the owner of a push to a default branch."""
        run = self.run
        if pushed.onto is not None:
            was = (pushed.replayed_from or "?")[:12]
            run.note(
                f"{workspace.branch} of {name} on origin had moved on to {pushed.onto[:12]}: the run's commits "
                f"were put on top of it, {was} became {pushed.head[:12]}, never forced.",
                repo=name,
                onto=pushed.onto,
                replayed_from=pushed.replayed_from,
            )
        if pushed.changed:
            run.note(
                f"Pushed {pushed.head[:12]} of {name} to {workspace.branch} on origin ({len(pushed.commits)} "
                "commit(s)).",
                commit_sha=pushed.head,
                repo=name,
            )
            if pushed.default:
                what = f"the push to {pushed.branch} of {name}"
                await push.send_notice(run, gitops.push_notice(run.id, name, pushed), what)
        else:
            run.note(f"{workspace.branch} of {name} on origin has {pushed.head[:12]} already: nothing to push.")

    def replay(self, name: str, workspace: gitops.Workspace) -> gitops.Replay:
        """How the push at the run's end puts the commits of ``name`` on top of a default branch the remote moved on:
        no verify runs again, since `evo-agents worker step` ran each done step's and pushed its commits, which the
        remote's branch has, and a commit it pushed that the remote dropped is not put back."""
        run = self.run
        return gitops.Replay(
            scratch=self.directory.path / runs.RESULT_DIR / f"replay-{workspace.worktree.name}",
            side_branch=gitops.SIDE_BRANCH.format(id=run.id),
            pushed=run.daemon.home.pushed(run.id, name, workspace.branch),
        )

    async def push_for_agent(self, name: str, title: str | None) -> dict:
        """Push the run's branch of repo ``name`` for the Builder's agent, which reported a step done and holds no
        credential: what is committed in its worktree, to its branch curator/... alone, with the run's lease, and on
        GitLab with the push options that open the merge request. The answer the agent's command prints, or why not."""
        run = self.run
        workspace = self.directory.workspaces.get(name)
        if not self.builder or workspace is None:
            return {"error": f"run {run.id} pushes no repo {name} for its agent"}
        try:
            head_branch = await gitops.current_branch(workspace.worktree)
            if head_branch != workspace.local_branch:
                return {
                    "error": f"{workspace.worktree} is on {head_branch or 'a detached HEAD'}, not "
                    f"{workspace.local_branch}: the daemon pushes the run's branch alone"
                }
            if self.forge_origin(name) and not run.credentials.covers(name):
                return {
                    "error": f"{name}: a run of the Curator pushes only with the credential the hub leased it, and no "
                    "lease covers its origin"
                }
            options = (
                judge.gitlab_push_options(self.targets.get(name) or "main", title)
                if run.curator.get("forge") == "gitlab"
                else []
            )

            async def pushed_branch() -> gitops.Pushed:
                with run.credentials.ticketed(run.daemon.env) as env:
                    return await gitops.push(
                        workspace.worktree,
                        workspace.branch,
                        protected=workspace.protected,
                        kind="curator",
                        plan_branch=None,
                        env=env,
                        options=options,
                    )

            async with self.push_lock:
                pushed = await run.credentials.with_renewal(name, pushed_branch)
        except gitops.GitError as exc:  # PushRefused among them
            return {"error": f"git push of {name} to {workspace.branch} failed: {exc}"}
        if pushed.changed:
            run.note(
                f"Pushed {pushed.head[:12]} of {name} to {workspace.branch} on origin for the agent "
                f"({len(pushed.commits)} commit(s)).",
                commit_sha=pushed.head,
                repo=name,
            )
        return {
            "branch": pushed.branch,
            "head": pushed.head,
            "default": pushed.default,
            "changed": pushed.changed,
            "commits": list(pushed.commits),
        }

    def forge_origin(self, name: str) -> bool:
        """Whether the origin of ``name`` is on a forge (https or SSH), which takes a credential; a path on this
        machine takes none."""
        urls = self.run.credentials.origins.get(name) or []
        return any(normalize_origin(url).startswith("https://") for url in urls)

    def push_options(self, name: str) -> list[str]:
        """The push options of a Builder's push to GitLab, which open its merge request; none otherwise."""
        if not self.builder or self.run.curator.get("forge") != "gitlab":
            return []
        return judge.gitlab_push_options(self.targets.get(name) or "main", self.run.title)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        return await self.directory.diff()

    async def release_branch(self) -> None:
        await self.directory.release()
