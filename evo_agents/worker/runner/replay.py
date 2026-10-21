"""A replay run (kind ``replay``, ``ReplayRun``): one step of the project's history done again, once with the harness
of the default branch (arm ``a``) and once with the harness of a change of the Curator (arm ``b``), to measure the
change (``evo_agents.hub.replay``).

The claim names the task (its plan and step, repo, what, verify, commits, and its base commit once a replay read it),
the change (its repo, branch, head and default branch) and the run's own key, which only this object holds. Once it
holds its leases, the worker's preflight looks for the program of the step's verify on the PATH that command runs with,
then it fetches the checkouts of the task's repo and of the change's, and reads (``replay_files``): the base commit,
the first parent of the oldest of the step's commits; the default branch's tip and the change's head; and the files the
change touches that are part of a harness, with every file of each skill among them. A task whose commits are not in
the checkout, or a change that touches no such file, is refused to the hub with why, and the run ends done without an
agent.

Each arm then gets a new directory under the run's own (``a/``, ``b/``): a repository of the task's repo that holds its
history up to the base commit and nothing after it, checked out there; the change's files as the arm's commit has them,
committed on top when the change is in that repo, else in a folder named as the change's repo; and each skill the
change touches, whole, under ``.claude/skills/`` of the directory, where the agent's runtime loads it. The agent, with
the runtime and model of the charter's Builder, starts in a new session there on the step's prompt
(``replay.replay_prompt``), with half the run's cost and agent time, and none of the hub's tools. When its turn is over
(or stopped short, which the arm notes), the worker runs the step's verify in the repository as code it does not trust
(``verify.run_untrusted``, as a judge run runs its checks), and posts the arm: passed when every command exited 0, the
cost, turns, tool calls and failed ones, the largest context and the agent's time
(POST /v1/worker/runs/{id}/replay, with the run's key). The run ends done with both arms' usage, pushing nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import curator, replay
from evo_agents.worker import gitops, untrusted
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner import access, origin, verify
from evo_agents.worker.runner import replay_files as files
from evo_agents.worker.runner.common import RunFailed, cut
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.sessions import LARGEST, add_usage
from evo_agents.worker.runner.transitions import MAX_SUMMARY_CHARS
from evo_agents.worker.runner.verify import verify_event

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

SKILLS = ".claude/skills"  # under an arm's directory: where the agent's runtime loads a skill from (author.SKILLS_DIR)
HARNESS_COMMIT = "The harness files this replay runs with"


class Refused(Exception):
    """The task cannot be replayed: ``reason``, and whether no replay of it ever can (``unusable``)."""

    def __init__(self, reason: str, *, unusable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.unusable = unusable


class ArmFigures:
    """What one arm's agent did, from its events: its turns, its tool calls and those that failed."""

    def __init__(self):
        self.turns: int | None = None
        self.calls: set[str] = set()
        self.failed: set[str] = set()

    def __call__(self, kind: str, body: dict) -> None:
        call = body.get("toolCallId")
        if kind == "tool_call" and isinstance(call, str):
            self.calls.add(call)
        elif kind == "tool_call_update" and body.get("status") == "failed" and isinstance(call, str):
            self.failed.add(call)
        elif kind == "usage_update" and isinstance(body.get("numTurns"), int):
            self.turns = (self.turns or 0) + body["numTurns"]


class ReplayRun:
    """A replay run: the task's base commit, the change's harness files, two arms each in a new repository at the base
    commit with its agent and the step's verify run on its work, each arm's result posted; nothing pushed."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        self.change = run.curator or {}
        found = self.change.get("replay") or {}
        self.replay_id = found.get("replay_id")
        self.task = found.get("task") or {}
        run.title = run.spec.get("title") or f"replay of {self.task.get('plan_id')}#{self.task.get('step_key')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=self.no_more)
        self.total = curator.budget_of(run.spec) or {}  # the run's caps, which the two arms share
        self.env = gitops.without_hooks(run.daemon.env)
        self.usages: list = []

    async def no_more(self) -> list[dict]:
        return []  # an arm's agent has one turn: nobody answers it

    async def steps(self) -> None:
        run, task = self.run, self.task
        names = access.repo_names([task.get("repo"), self.harness_repo])
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: the replay of step {task.get('step_key')} of "
            f"plan {task.get('plan_id')} for the Curator's change #{self.change.get('change_id')}, {run.runtime}, "
            f"timeout {run.timeout_s // 60} min of agent time. Nothing is committed to origin or pushed."
        )
        cls = run.agent.runtime_class()
        if not task.get("repo") or not task.get("verify") or not self.harness_repo:
            raise RunFailed("the claim of a replay run names no task, or no change")
        await access.take_credentials(run, names)
        run.check()
        await access.check(run, names, path=untrusted.scrubbed_env(run.daemon.env).get("PATH"), verify=task["verify"])
        run.check()
        try:
            plan = await self.prepare()
        except Refused as exc:
            await self.post({"refused": cut(exc.reason, replay.MAX_REASON_CHARS), "task_unusable": exc.unusable})
            await self.end(f"Nothing replayed: {exc.reason}.")
            return
        results = {}
        for arm in replay.ARMS:
            run.check()
            results[arm] = await self.arm(cls, arm, plan)
            await self.post({"arm": arm, "base_sha": plan["base"], "result": results[arm]})
        words = ", ".join(f"arm {arm} {'passed' if results[arm]['passed'] else 'failed'}" for arm in replay.ARMS)
        await self.end(f"Replayed step {task.get('step_key')} of plan {task.get('plan_id')}: {words}.")

    @property
    def harness_repo(self) -> str | None:
        """The change's repo, whose harness files the arms run with."""
        return (self.change.get("replay") or {}).get("repo")

    def checkout(self, name: str) -> Path:
        found = self.run.daemon.checkout_for(self.run.project, name)
        if found is None:
            raise RunFailed(f"this worker has no checkout of {self.run.project}/{name}")
        return found

    async def fetched(self, name: str) -> Path:
        run, checkout = self.run, self.checkout(name)
        async with run.daemon.repo_lock(checkout):
            if not await gitops.has_remote(checkout):
                raise RunFailed(f"the checkout at {checkout} has no remote named origin to fetch from")
            await origin.fetch(run, checkout)
        return checkout

    async def prepare(self) -> dict:
        """What both arms start from: the base commit, the default branch's tip and the change's head, and the files of
        the change each arm writes; Refused when the task or the change gives nothing to replay."""
        task = self.task
        self.directory.claim("queue the replay again")
        self.directory.enter()
        self.directory.save()
        source = await self.fetched(task["repo"])
        harness = source if self.harness_repo == task["repo"] else await self.fetched(self.harness_repo)
        try:
            return await self.read(source, harness)
        except gitops.GitError as exc:
            raise RunFailed(f"the replay's commits were not read: {exc}", cause="checkout") from None

    async def read(self, source: Path, harness: Path) -> dict:
        """``prepare``'s reading of the checkouts ``source`` (the task's repo) and ``harness`` (the change's)."""
        run, task, change = self.run, self.task, self.change
        base = await files.commit_of(source, task["base_sha"], self.env) if task.get("base_sha") else None
        base = base or await files.base_commit(source, task.get("commits") or [], self.env)
        if base is None:
            raise Refused(
                f"none of the step's commits ({', '.join(task.get('commits') or [])}) is on origin of {task['repo']}, "
                "or the oldest has no parent: the task has no base commit",
                unusable=True,
            )
        branch = change.get("base_branch") or await gitops.remote_default_branch(harness)
        branch = branch or default_branch_of(run.daemon.config, run.project, self.harness_repo) or "main"
        default = await files.commit_of(harness, f"refs/remotes/origin/{branch}", self.env)
        head = await files.commit_of(harness, change["head_sha"], self.env) if change.get("head_sha") else None
        head = head or await files.commit_of(harness, f"refs/remotes/origin/{change.get('branch')}", self.env)
        if default is None or head is None:
            raise Refused(f"origin of {self.harness_repo} has no {branch if default is None else change.get('branch')}")
        paths = await files.harness_of(harness, default, head, self.env)
        if not paths:
            raise Refused(f"the change touches no skill, AGENTS.md or CLAUDE.md file of {self.harness_repo}")
        skills = sorted({found for found in map(replay.skill_dir, paths) if found})
        whole = set(paths)
        for ref in (default, head):
            for skill in skills:
                whole.update(await files.files_under(harness, ref, skill, self.env))
        run.note(
            f"Replay from {base[:12]}, the base commit of the step, with {len(paths)} file(s) of the change "
            f"({', '.join(paths[:5])}{', ...' if len(paths) > 5 else ''}): arm a at {default[:12]} of {branch}, arm b "
            f"at {head[:12]} of {change.get('branch')}.",
            base_sha=base,
        )
        return {
            "base": base,
            "source": source,
            "harness": harness,
            "refs": {"a": default, "b": head},
            "paths": paths,
            "whole": sorted(whole),
            "skills": skills,
        }

    async def lay_out(self, arm: str, plan: dict) -> tuple[Path, Path, str | None]:
        """The arm's directory, its repository of the task's repo at the base commit, and the folder of the change's
        files when the change is in another repo."""
        run, task = self.run, self.task
        root = self.directory.path / arm
        folder = gitops.folder_name(task["repo"])
        repository = root / folder
        ref = plan["refs"][arm]
        try:
            await files.standalone(plan["source"], plan["base"], repository, self.env)
            pairs = [(path, path) for path in plan["whole"]]
            harness_folder = None
            if self.harness_repo == task["repo"]:
                await files.write_at(plan["harness"], ref, pairs, repository, self.env)
                await files.commit_all(repository, HARNESS_COMMIT, self.env)
            else:
                harness_folder = gitops.folder_name(self.harness_repo, {folder})
                await files.write_at(plan["harness"], ref, pairs, root / harness_folder, self.env)
            for skill in plan["skills"]:
                name = skill.rsplit("/", 1)[-1]
                under = [
                    (path, f"{name}/{path[len(skill) + 1 :]}") for path in plan["whole"] if path.startswith(skill + "/")
                ]
                await files.write_at(plan["harness"], ref, under, root / SKILLS, self.env)
        except (gitops.GitError, OSError) as exc:
            raise RunFailed(f"arm {arm}: the replay's directory {root} was not made: {exc}", cause="checkout") from None
        (root / ".evo-run").mkdir(mode=0o700, exist_ok=True)
        run.note(
            f"Arm {arm}: {repository} at {plan['base'][:12]}, the change's files at {ref[:12]}.", worktree=str(root)
        )
        return root, repository, harness_folder

    def arm_budget(self) -> dict | None:
        """The caps of the next arm's agent: half the run's cost and half its agent time, from now; its turns."""
        total = self.total
        if not total:
            return None
        run = self.run
        found = {key: total.get(key) for key in curator.BUDGET_KEYS}
        if found.get("max_usd") is not None:
            found["max_usd"] = round(found["max_usd"] / 2, 4)
        if found.get("max_seconds") is not None:
            found["max_seconds"] = int(run.watchdog.spent_before + run.agent_seconds() + found["max_seconds"] / 2)
        return {**found, "spent_usd": 0.0, "spent_seconds": 0.0}

    async def arm(self, cls, arm: str, plan: dict) -> dict:
        """One arm: its directory, its agent, then the step's verify on its work; what it found."""
        run, task = self.run, self.task
        root, repository, harness_folder = await self.lay_out(arm, plan)
        run.worktree = root
        budget = self.arm_budget()
        if budget is not None:
            run.spec["budget"] = budget
        run.watchdog.spent_usd = 0.0  # a new session, whose cost starts at nothing
        figures = ArmFigures()
        run.listeners.append(figures)
        prompt = replay.replay_prompt(run.project, task, folder=repository.name, harness_folder=harness_folder)
        started, error, usage = run.loop.time(), None, None
        try:
            await self.conversation.turns(cls, prompt, None)
            usage = run.outcome.usage if run.outcome else None
        except RunFailed as exc:
            error, usage = exc.error, exc.fields.get("usage")
            run.note(f"Arm {arm}: the agent's turn did not end ({exc.error}); its work is verified as it is.")
        finally:
            run.listeners.remove(figures)
        duration_ms = int((run.loop.time() - started) * 1000)
        self.usages.append(usage)
        checks = await self.verify(arm, repository)
        return {
            "passed": bool(checks) and all(item["exit_code"] == 0 for item in checks),
            "harness_sha": plan["refs"][arm],
            "harness_paths": plan["paths"][: replay.MAX_PATHS],
            "verify": checks,
            "cost_usd": _cost(usage),
            "turns": figures.turns,
            "tool_calls": len(figures.calls),
            "tool_errors": len(figures.failed),
            "peak_context_tokens": _whole((usage or {}).get("peak_context_tokens")),
            "duration_ms": duration_ms,
            "agent_error": cut(error, replay.MAX_REASON_CHARS) if error else None,
            "session_id": self.conversation.session_id,
        }

    async def verify(self, arm: str, repository: Path) -> list[dict]:
        """Run the step's verify on the arm's work, as code the worker does not trust: each one's exit code."""
        run = self.run
        results = []
        for command in self.task.get("verify") or []:
            run.check()
            started = run.loop.time()
            code, output = await verify.run_untrusted(run, command, repository)
            duration_ms = int((run.loop.time() - started) * 1000)
            results.append({"exit_code": code, "duration_ms": duration_ms})
            run.event("system", {**verify_event(command, code, duration_ms, output), "arm": arm})
        return results

    async def post(self, body: dict) -> None:
        run = self.run
        try:
            await run.daemon.hub.replay(run.id, body, run.replay_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not take the replay's result: {exc}") from None

    async def end(self, summary: str) -> None:
        """End the run done, with the usage of both arms' sessions added up."""
        run = self.run
        run.summary = summary
        run.note(summary)
        usage = None
        for found in self.usages:
            usage = add_usage(usage, found, largest=LARGEST)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        await run.reports.end("done", summary=cut(summary, MAX_SUMMARY_CHARS), usage=usage)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return []  # its repositories are its own: the watchdog compares no protected path, only the caps

    async def diff(self) -> bytes:
        """A replay run changes nothing of the project: no diff is uploaded."""
        return b""

    async def release_branch(self) -> None:
        return None  # it checks out no branch of a checkout


def _cost(usage) -> float | None:
    for key in ("total_cost_usd", "cost"):
        value = (usage or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(float(value), 0.0)
    return None


def _whole(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None
