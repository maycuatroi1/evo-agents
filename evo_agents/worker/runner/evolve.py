"""An evolve run (kind ``evolve``, ``EvolveRun``): the Curator's evolution of a harness, measured on light tasks, the
best candidate written on a branch (``evo_agents.hub.evolve``, ``evolve_loop``).

The claim names the run's own key (``X-Evo-Evolve-Key``), which only the Run holds; with it the worker reads what the
run evolves (GET /v1/worker/runs/{id}/evolve): the target (the harness files of a repo at a base commit, ``git``, or
an agent's instructions and skill, ``agent_config``), the baseline's components (for git, each path read at the base
commit of the checkout here when the hub sends none, every file of a skill's directory with it), the light tasks of the
select split and the active probes, each with its prompt, base commit and verify, the baseline arms the hub's cache
holds, and the budget. Once it holds its leases, the preflight looks for the program of each verify on the PATH those
commands run with, and for a git target checks it can push to the repo.

The evolution runs in the run's directory: its tree under ``evolution/`` (``evolve_workspace``), where the proposer
agent works, and each task's directory under ``evals/``, a new repository of the task's repo at the task's base commit
with the candidate's files placed (a skill's files also under ``.claude/skills/``; an agent's instructions as the
directory's CLAUDE.md and AGENTS.md, its skill under ``.claude/skills/``). Each agent session starts new on the run's
runtime and model, with what is left of the run's caps, none of the hub's tools and no remote to push to; the worker
runs each verify itself as code it does not trust (``verify.run_untrusted``), in the task's repository, each path of it
into a developer's checkout of a repo (``~/github/<repo>``) written as where that repo is here (``localized``: the
task's repository for the evolution's repo, this worker's checkout for another repo of the project), and no event carries a verify command or
its output. The proposer never sees a verify command or a holdout task: neither is in the tree.

Once the loop ends, for a git target the best candidate, when it differs from the baseline, is committed on top of
the base commit in a worktree of the checkout and pushed to the run's branch ``curator/...`` (never forced, never a
default branch); the hub is told the best candidate, the commit and the branch (POST .../evolve/report), and the run
ends done. A run that fails tells the hub so too.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import curator, evolve, replay
from evo_agents.worker import gitops, untrusted
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner import access, verify
from evo_agents.worker.runner import evolve_eval as measure
from evo_agents.worker.runner import replay_files as files
from evo_agents.worker.runner.common import RunFailed, cut, push_cause
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.evolve_loop import Candidate, Evolution
from evo_agents.worker.runner.evolve_workspace import Tree
from evo_agents.worker.runner.replay import HARNESS_COMMIT, SKILLS, ArmFigures, ReplayRun, usage_cost

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

TREE = "evolution"  # under the run's directory: the tree the proposer works in
EVALS = "evals"  # under the run's directory: each task's directory
BEST = "best"  # under the run's directory: the worktree the best candidate is committed in
VERIFY_KEEP = 4 * 1024 * 1024  # bytes of a verify's output the trace keeps: all of it, short of a runaway command
MAX_TOOL_ERRORS = 50  # failed tool calls a session's trace holds
MAX_TOOL_ERROR_CHARS = 4000
MAX_REASON_CHARS = 2000


class SessionFigures(ArmFigures):
    """What one session's agent did, from its events: its turns and tool calls (``ArmFigures``), and the text of each
    failed one."""

    def __init__(self):
        super().__init__()
        self.titles: dict[str, str] = {}
        self.errors: list[str] = []

    def __call__(self, kind: str, body: dict) -> None:
        super().__call__(kind, body)
        call = body.get("toolCallId")
        if kind == "tool_call" and isinstance(call, str):
            self.titles[call] = str(body.get("title") or "")
        elif kind == "tool_call_update" and body.get("status") == "failed" and len(self.errors) < MAX_TOOL_ERRORS:
            texts = [
                ((item.get("content") or {}).get("text") or "")
                for item in body.get("content") or []
                if isinstance(item, dict) and isinstance(item.get("content"), dict)
            ]
            text = "\n".join(text for text in texts if text) or str(body.get("rawOutput") or "")
            self.errors.append(cut(f"{self.titles.get(str(call), 'a tool')}: {text}", MAX_TOOL_ERROR_CHARS))


def _hub(run: Run):
    """The hub of the worker that runs ``run``; RunFailed for a worker signed in to none."""
    hub = run.daemon.hub
    if hub is None:
        raise RunFailed("the worker is signed in to no hub")
    return hub


class EvolveHub:
    """The hub's side of the evolution (``evolve_loop.Hub``), with the run's own key."""

    def __init__(self, run: Run):
        self.run = run

    async def arm(self, body: dict) -> dict:
        try:
            return await _hub(self.run).evolve_arm(self.run.id, body, self.run.evolve_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not take a baseline arm: {exc}") from None

    async def candidate(self, body: dict) -> dict:
        try:
            return await _hub(self.run).evolve_candidate(self.run.id, body, self.run.evolve_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not take a candidate: {exc}") from None


class RunSeams:
    """What the evolution does through the run (``evolve_eval.Seams``): the directory of a task, the agent and the
    verify."""

    def __init__(self, owner: EvolveRun):
        self.owner = owner
        self.run = owner.run

    async def lay_out(self, task: Mapping, components: Mapping[str, str | None], name: str) -> tuple[Path, Path]:
        owner = self.owner
        root = owner.root() / EVALS / name
        try:
            repository = await owner.repository_of(task, root)
            if owner.target == evolve.TARGETS[1]:
                place_agent(root, owner.inputs.get("agent"), components)
            else:
                place_files(root, repository, components)
            await files.commit_all(repository, HARNESS_COMMIT, owner.env)
        except (gitops.GitError, OSError) as exc:
            raise RunFailed(f"the directory {root} of a task was not made: {exc}", cause="checkout") from None
        (root / ".evo-run").mkdir(mode=0o700, exist_ok=True)
        return root, repository

    async def agent(self, directory: Path, prompt: str) -> measure.Session:
        run, owner = self.run, self.owner
        run.check()
        run.worktree = directory
        budget = owner.session_budget()
        if budget is not None:
            run.spec["budget"] = budget
        run.watchdog.spent_usd = 0.0  # a new session, whose cost starts at nothing
        figures = SessionFigures()
        run.listeners.append(figures)
        error, usage, summary = None, None, None
        try:
            await owner.conversation.turns(owner.cls, prompt, None)
            usage = run.outcome.usage if run.outcome else None
            summary = run.outcome.summary if run.outcome else None
        except RunFailed as exc:
            error, usage = exc.error, exc.fields.get("usage")
            run.note(f"An agent's turn in {directory.name} did not end ({exc.error}); its work is taken as it is.")
        finally:
            run.listeners.remove(figures)
        owner.usages.append(usage)
        cost = usage_cost(usage)
        owner.spent_usd += cost or 0.0
        reason = cut(error, MAX_REASON_CHARS) if error else None
        return measure.Session(cost, figures.turns, figures.errors, reason, summary)

    async def verify(self, command: str, cwd: Path) -> measure.Checked:
        """``command`` run by the worker as code it does not trust; the event says its exit code alone."""
        run = self.run
        run.check()
        command = await self.owner.localized(command, self.owner.harness_repo, cwd)
        started = run.loop.time()
        code, output = await verify.run_untrusted(run, command, cwd, keep=VERIFY_KEEP)
        duration_ms = int((run.loop.time() - started) * 1000)
        text = f"A task's verify in {cwd.parent.name} exited {code} in {duration_ms / 1000:.1f}s."
        run.event("system", {"text": text, "exit_code": code, "duration_ms": duration_ms})
        return measure.Checked(code, output)


def place_files(root: Path, repository: Path, components: Mapping[str, str | None]) -> None:
    """A git candidate's files in ``repository`` (None removes one), and each skill's files under .claude/skills/ of
    ``root``, where the agent's runtime loads them."""
    files.write_texts(components, repository)
    skills = sorted({found for found in map(replay.skill_dir, components) if found})
    for skill in skills:
        short = skill.rsplit("/", 1)[-1]
        under = {
            f"{short}/{path[len(skill) + 1 :]}": text
            for path, text in components.items()
            if path.startswith(skill + "/")
        }
        files.write_texts(under, root / SKILLS)


def place_agent(root: Path, agent, components: Mapping[str, str | None]) -> None:
    """An agent_config candidate in ``root``: its instructions as CLAUDE.md and AGENTS.md, its skill as
    .claude/skills/<agent>/SKILL.md."""
    instructions, skill = (components.get(axis) for axis in evolve.AGENT_AXES)
    files.write_texts({"CLAUDE.md": instructions, "AGENTS.md": instructions}, root)
    name = gitops.folder_name(str(agent or "agent"))
    files.write_texts({f"{name}/SKILL.md": skill}, root / SKILLS)


async def write_branch(
    checkout: Path,
    base: str,
    worktree: Path,
    texts: Mapping[str, str | None],
    message: str,
    push: Callable[[Path], Awaitable[gitops.Pushed]],
) -> gitops.Pushed | None:
    """``texts`` committed on ``base`` in a new worktree of ``checkout`` at ``worktree`` and pushed with ``push``;
    what was pushed, None when they change nothing. The worktree is removed after."""
    await gitops.add_detached_worktree(checkout, worktree, base)
    try:
        if await measure.commit_candidate(worktree, texts, message) is None:
            return None
        return await push(worktree)
    finally:
        with contextlib.suppress(gitops.GitError, OSError):
            await gitops.remove_worktree(checkout, worktree)


class EvolveRun(ReplayRun):
    """An evolve run: the inputs read with its key, the evolution in its directory, the best candidate written on its
    branch and reported. It lays out each task as a replay run lays out an arm, and ends as one."""

    def __init__(self, run: Run):
        self.run = run
        self.change = run.curator or {}
        self.task = {}
        self.inputs: dict = {}
        self.target = evolve.TARGETS[0]
        run.title = run.spec.get("title") or f"evolution of run #{run.id}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=self.no_more)
        self.total = _budget(run.spec)  # the run's caps, which its sessions share as they go
        self.env = gitops.without_hooks(run.daemon.env)
        self.usages: list = []
        self.siblings: set[str] = set()
        self.spent_usd = 0.0
        self.source: Path | None = None
        self.cls = None

    @property
    def harness_repo(self) -> str | None:
        return self.inputs.get("repo")

    async def steps(self) -> None:
        run = self.run
        self.cls = run.agent.runtime_class()
        self.inputs = await self.read_inputs()
        self.target = self.inputs.get("target") or evolve.TARGETS[0]
        try:
            best = await self.evolve()
            pushed = await self.write_best(best)
        except RunFailed as exc:
            await self.report({"state": "failed", "reason": cut(exc.error, MAX_REASON_CHARS)}, quiet=True)
            raise
        head, branch = (pushed.head, pushed.branch) if pushed else (None, None)
        reason = None if pushed or best is None else "no candidate changed the baseline's files on the branch"
        body = {"state": "done", "best_candidate_id": best.id if best else None, "head_sha": head, "branch": branch}
        await self.report({**body, "reason": reason})
        where = f", pushed {head[:12]} to {branch}" if head else ""
        total = sum(best.scores.values()) if best else 0
        await self.end(
            f"Evolved {self.target} over {len(self.inputs.get('tasks') or [])} task(s): best candidate "
            f"{best.id if best else 'none'} at {total:g}{where}."
        )

    async def read_inputs(self) -> dict:
        run = self.run
        try:
            found = await _hub(run).evolve_inputs(run.id, run.evolve_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not answer what the evolve run evolves: {exc}") from None
        if not isinstance(found, dict) or found.get("target") not in evolve.TARGETS:
            raise RunFailed("the hub's answer of what the evolve run evolves names no target it knows")
        found.setdefault("runtime", run.runtime)
        found.setdefault("model", run.spec.get("model"))
        found["runtime"] = found.get("runtime") or run.runtime
        return found

    async def evolve(self) -> Candidate | None:
        run, inputs = self.run, self.inputs
        tasks = [task for task in inputs.get("tasks") or [] if isinstance(task, dict) and measure.usable(task)]
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: evolution #{inputs.get('evolve_id')} of the "
            f"{self.target} target {inputs.get('repo') or inputs.get('agent') or ''}, {len(tasks)} task(s), "
            f"{run.runtime}, timeout {run.timeout_s // 60} min of agent time."
        )
        if not tasks:
            raise RunFailed("the evolve run has no task of the select split or probe to measure on")
        names = access.repo_names([inputs.get("repo")])
        await access.take_credentials(run, names)
        run.check()
        commands = [command for task in tasks for command in measure.verify_commands(task)]
        pushes = names if self.target == evolve.TARGETS[0] and inputs.get("branch") else []
        path = untrusted.scrubbed_env(run.daemon.env).get("PATH")
        await access.check(run, names, path=path, verify=commands, pushes=pushes)
        run.check()
        await self.prepare_evolution()
        tree = Tree(self.root() / TREE)
        evolution = Evolution(inputs, EvolveHub(run), RunSeams(self), tree, note=run.note, check=run.check)
        return await evolution.run()

    async def prepare_evolution(self) -> None:
        """The run's directory, the checkout fetched, the base commit read and, for git, the baseline when the hub
        sent none."""
        inputs = self.inputs
        self.directory.claim("queue the evolution again")
        self.directory.enter()
        self.directory.save()
        if not inputs.get("repo"):
            return
        self.source = await self.fetched(inputs["repo"])
        base = await files.commit_of(self.source, inputs.get("base_sha") or "HEAD", self.env)
        if base is None:
            raise RunFailed(f"origin of {inputs['repo']} has no commit {inputs.get('base_sha')}, the evolution's base")
        inputs["base_sha"] = base
        if self.target == evolve.TARGETS[0] and not inputs.get("baseline"):
            paths = [path for path in inputs.get("paths") or [] if isinstance(path, str)]
            try:
                inputs["baseline"] = await measure.baseline_at(self.source, base, paths, self.env)
            except gitops.GitError as exc:
                raise RunFailed(f"the baseline's files were not read: {exc}", cause="checkout") from None
            self.run.note(f"The baseline: {len(inputs['baseline'])} file(s) at {base[:12]}.", base_sha=base)

    async def repository_of(self, task: Mapping, root: Path) -> Path:
        """A new repository for ``task`` under ``root``: of the evolution's repo at the task's base commit (else the
        evolution's), or an empty one when it evolves no repo."""
        if self.source is None:
            repository = root / "work"
            repository.mkdir(parents=True)
            await gitops.git(repository, "init", "--quiet", env=self.env)
            return repository
        base = await files.commit_of(self.source, task["base_sha"], self.env) if task.get("base_sha") else None
        repository = root / gitops.folder_name(self.inputs["repo"])
        await files.standalone(self.source, base or self.inputs["base_sha"], repository, self.env)
        return repository

    def root(self) -> Path:
        """The run's directory, entered before the evolution starts (``prepare``)."""
        if self.directory.path is None:
            raise RunFailed("the run's directory is not made yet")
        return self.directory.path

    def session_budget(self) -> dict | None:
        """The caps of the next session: what is left of the run's cost, the run's agent time (counted over its
        sessions) and its turns."""
        total = self.total
        if not total:
            return None
        found = {key: total.get(key) for key in curator.BUDGET_KEYS}
        usd = found.get("max_usd")
        if usd is not None:
            found["max_usd"] = round(max(usd - self.spent_usd, curator.MIN_RUN_USD), 4)
        return {**found, "spent_usd": 0.0, "spent_seconds": 0.0}

    async def write_best(self, best: Candidate | None) -> gitops.Pushed | None:
        """For a git target, the best candidate committed on the base commit and pushed to the run's branch, when it
        differs from the baseline; None otherwise."""
        run, inputs = self.run, self.inputs
        baseline = inputs.get("baseline") or {}
        if best is None or self.target != evolve.TARGETS[0] or best.components == baseline or self.source is None:
            return None
        branch, repo = inputs.get("branch"), inputs["repo"]
        if not branch:
            raise RunFailed("the evolve run names no branch to write its best candidate on")
        default = await gitops.remote_default_branch(self.source) or default_branch_of(
            run.daemon.config, run.project, repo
        )
        message = (
            f"curator: evolved {', '.join(sorted(best.components)[:5])}\n\nCandidate {best.id} of evolution "
            f"#{inputs.get('evolve_id')}, {sum(best.scores.values()):g} over {len(best.scores)} task(s)."
        )

        async def push(worktree: Path) -> gitops.Pushed:
            async def pushed() -> gitops.Pushed:
                with run.credentials.ticketed(run.daemon.env) as env:
                    protected = tuple(name for name in (default,) if name)
                    return await gitops.push(worktree, branch, protected=protected, kind="curator", env=env)

            return await run.credentials.with_renewal(repo, pushed)

        texts = measure.placed(baseline, best.components)
        try:
            async with run.daemon.repo_lock(self.source):
                found = await write_branch(self.source, inputs["base_sha"], self.root() / BEST, texts, message, push)
        except gitops.PushRefused as exc:
            raise RunFailed(f"{repo}: {exc}") from None
        except gitops.GitError as exc:
            raise RunFailed(f"the best candidate was not pushed to {branch}: {exc}", cause=push_cause(exc)) from None
        if found is not None:
            run.note(f"Pushed {found.head[:12]}, candidate {best.id}, to {branch} of {repo}.", commit_sha=found.head)
        return found

    async def report(self, body: dict, *, quiet: bool = False) -> None:
        run = self.run
        try:
            await _hub(run).evolve_report(run.id, body, run.evolve_key)
        except HubProblem as exc:
            if not quiet:
                raise RunFailed(f"the hub did not take the evolution's report: {exc}") from None
            run.note(f"The hub did not take the evolution's failure: {exc}")


def _budget(spec: Mapping) -> dict:
    return curator.budget_of(spec) or {}
