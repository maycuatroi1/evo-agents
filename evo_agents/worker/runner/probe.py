"""A probe run (kind ``probe``, ``ProbeRun``): one probe of a change of the Curator measured on one runtime and model, k
new agent sessions with the change's rule on (arm ``on``) and k with it off (arm ``off``), to tell whether the rule
still keeps the correction it was made for from coming back (``evo_agents.hub.probes``).

The claim names the probe (its repo, base commit, task, verify, k and week), the change (its repo, merge commit or head,
and default branch) and the run's own key, which only this object holds. Once it holds its leases, the worker's
preflight looks for the program of the probe's verify on the PATH that command runs with, then it fetches the checkouts
of the probe's repo and of the change's, and reads (``replay_files``): the base commit (the probe's, else the commit
of the default branch's first-parent line that was its tip when the probe was made); the tip of the default branch of
the change's repo, which arm on runs with; and a commit of the change's repo whose tree is that tip less the change's
diff (its merge commit against its first parent, else its head against its merge base), applied in reverse in an index
of its own, which arm off runs with. The diff taken off is that of the files a probe arm places
(``probes.placed_paths``). A change that places no such file, or whose diff no longer comes off the tip, makes the probe
stale for the week: the worker posts why and the run ends done without an agent.

Each session lays out a new directory under the run's own (``on-1/``, ``off-1/``, ...) as a replay arm does
(``ReplayRun.lay_out``): a repository of the probe's repo at the base commit, the harness files of the change's repo as
the arm's commit has them, and each skill under ``.claude/skills/``; on claude-code also the repo's
``.claude/settings.json`` and ``.claude/hooks/`` there, where Claude Code loads them. The agent, on the run's runtime
and model, starts in a new session on the probe's task alone (``probes.probe_prompt``), with 1/2k of the run's cost and
agent time, none of the hub's tools and no remote to push to. When its turn is over (or stopped short, which the
session notes), the worker writes the commands the agent ran and the files it changed to a trace file outside the
session's directory, read-only, and runs the probe's verify in the repository as code it does not trust
(``verify.run_untrusted``), with the trace file named in EVO_PROBE_TRACE. It posts the session: passed when the verify
exited 0, its cost, turns and time, and the runtime and the model it ran on (POST /v1/worker/runs/{id}/probe, with the
run's key). The sessions alternate between the arms. No event of the run carries the verify or its output: the agent of
a later run reads the run's events. The run ends done with every session's usage, pushing nothing.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import curator, probes, replay
from evo_agents.worker import gitops, untrusted
from evo_agents.worker.checkouts import default_branch_of
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner import access, verify
from evo_agents.worker.runner import replay_files as files
from evo_agents.worker.runner.common import RunFailed, cut
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.replay import ArmFigures, Refused, ReplayRun, _cost

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

TRACES = "traces"  # under the run's directory, beside the sessions' own: the trace file of each session
SCRATCH = "scratch"  # under the run's directory: the index arm off's commit is made in


class SessionFigures(ArmFigures):
    """What one session's agent did, from its events: its turns and tool calls (``ArmFigures``), the commands it ran
    and the model its runtime said it runs on."""

    def __init__(self):
        super().__init__()
        self.commands: list[str] = []
        self.model: str | None = None

    def __call__(self, kind: str, body: dict) -> None:
        super().__call__(kind, body)
        if kind == "tool_call" and body.get("kind") == "execute":
            command = (body.get("rawInput") or {}).get("command")
            if isinstance(command, str) and len(self.commands) < probes.MAX_COMMANDS:
                self.commands.append(command[: probes.MAX_COMMAND_CHARS])
        elif kind == "output" and isinstance(body.get("raw"), dict):
            model = body["raw"].get("model")
            if isinstance(model, str) and model and self.model is None:
                self.model = model[:200]


class ProbeRun(ReplayRun):
    """A probe run: the probe's base commit, the tip of the change's repo and that tip less the change's diff, k
    sessions of each arm each in a new repository at the base commit with its agent and the probe's verify run on its
    work and a trace of it, each session's result posted; nothing pushed. It lays its sessions out as a replay run
    lays out its arms."""

    def __init__(self, run: Run):
        self.run = run
        self.change = run.curator or {}
        self.probe = self.change.get("probe") or {}
        probe = self.probe
        self.task = {"repo": probe.get("repo"), "verify": [probe["verify"]] if probe.get("verify") else []}
        run.title = run.spec.get("title") or f"probe #{probe.get('probe_id')} of change #{self.change.get('change_id')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=self.no_more)
        self.total = curator.budget_of(run.spec) or {}  # the run's caps, which the 2k sessions share
        self.env = gitops.without_hooks(run.daemon.env)
        self.usages: list = []
        self.k = max(1, min(int(probe.get("k") or probes.DEFAULT_K), probes.MAX_K))

    @property
    def harness_repo(self) -> str | None:
        """The change's repo, which holds the rule the arms put on and take off."""
        return (self.change.get("probe") or {}).get("change_repo")

    async def steps(self) -> None:
        run, probe = self.run, self.probe
        names = access.repo_names([probe.get("repo"), self.harness_repo])
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: probe #{probe.get('probe_id')} of the Curator's "
            f"change #{self.change.get('change_id')}, {self.k} session(s) with its rule on and {self.k} with it off, "
            f"{run.runtime}, timeout {run.timeout_s // 60} min of agent time. Nothing is committed to origin or pushed."
        )
        cls = run.agent.runtime_class()
        if not probe.get("repo") or not probe.get("verify") or not self.harness_repo:
            raise RunFailed("the claim of a probe run names no probe, or no change")
        await access.take_credentials(run, names)
        run.check()
        path = untrusted.scrubbed_env(run.daemon.env).get("PATH")
        await access.check(run, names, path=path, verify=[probe["verify"]])
        run.check()
        try:
            plan = await self.prepare()
        except Refused as exc:
            await self.post({"stale": cut(exc.reason, probes.MAX_REASON_CHARS)})
            await self.end(f"Nothing probed: the probe is stale this week, {exc.reason}.")
            return
        passed = dict.fromkeys(probes.ARMS, 0)
        for index in range(1, self.k + 1):
            for arm in probes.ARMS:
                run.check()
                session = await self.session(cls, arm, index, plan)
                passed[arm] += session["passed"]
                await self.post({"base_sha": plan["base"], "harness_sha": plan["refs"]["on"], "session": session})
        words = ", ".join(f"arm {arm} passed {passed[arm]} of {self.k}" for arm in probes.ARMS)
        await self.end(f"Probed #{probe.get('probe_id')} on {run.runtime}: {words}.")

    async def read(self, source: Path, harness: Path) -> dict:
        """``prepare``'s reading of the checkouts ``source`` (the probe's repo) and ``harness`` (the change's); Refused,
        the probe stale, when arm off cannot be made."""
        run, change = self.run, self.change
        base = await self.base_of(source)
        branch = change.get("base_branch") or await gitops.remote_default_branch(harness)
        branch = branch or default_branch_of(run.daemon.config, run.project, self.harness_repo) or "main"
        tip = await files.commit_of(harness, f"refs/remotes/origin/{branch}", self.env)
        if tip is None:
            raise Refused(f"origin of {self.harness_repo} has no {branch}")
        new, old = await self.diff_of(harness, tip)
        paths = probes.placed_paths(await files.changed_between(harness, old, new, self.env), run.runtime)
        if not paths:
            raise Refused(f"the change places no harness file of {self.harness_repo} a probe on {run.runtime} places")
        try:
            off = await files.without_diff(harness, tip, old, new, paths, self.directory.path / SCRATCH, self.env)
        except gitops.GitError as exc:
            raise Refused(f"the change's diff no longer comes off {branch} at {tip[:12]} ({exc})") from None
        skills = sorted({found for found in map(replay.skill_dir, paths) if found})
        claude = run.runtime == probes.CLAUDE_CODE  # its arms carry the repo's settings and hooks, whole
        whole = set(paths) | ({probes.SETTINGS_FILE} if claude else set())
        for ref in (tip, off):
            for directory in (*skills, *([probes.HOOKS_DIR] if claude else [])):
                whole.update(await files.files_under(harness, ref, directory, self.env))
        run.note(
            f"Probe from {base[:12]}, with {len(paths)} file(s) of the change ({', '.join(paths[:5])}"
            f"{', ...' if len(paths) > 5 else ''}): arm on at {tip[:12]} of {branch}, arm off at {off[:12]}, that tip "
            "less the change's diff.",
            base_sha=base,
        )
        return {
            "base": base,
            "source": source,
            "harness": harness,
            "refs": {"on": tip, "off": off},
            "paths": paths,
            "whole": sorted(whole),
            "skills": skills,
        }

    async def base_of(self, source: Path) -> str:
        """The probe's base commit: its own, else the commit of the default branch's first-parent line that was its tip
        when the probe was made."""
        run, probe = self.run, self.probe
        if probe.get("base_sha"):
            found = await files.commit_of(source, probe["base_sha"], self.env)
            if found is None:
                raise Refused(f"origin of {probe['repo']} has no commit {probe['base_sha'][:12]}, the probe's base")
            return found
        branch = await gitops.remote_default_branch(source)
        branch = branch or default_branch_of(run.daemon.config, run.project, probe["repo"]) or "main"
        made = probe.get("made_at")
        when = made.isoformat() if isinstance(made, datetime) else str(made or "")
        found = (
            await files.first_parent_before(source, f"refs/remotes/origin/{branch}", when, self.env) if when else None
        )
        if found is None:
            raise Refused(f"{branch} of {probe['repo']} has no commit before the probe was made ({when or 'unknown'})")
        return found

    async def diff_of(self, harness: Path, tip: str) -> tuple[str, str]:
        """(new, old) of the change's diff: its merge commit and that commit's first parent, else its head and the
        merge base of its head with ``tip``."""
        probe, change = self.probe, self.change
        merge = probe.get("merge_sha")
        if merge:
            new = await files.commit_of(harness, merge, self.env)
            old = await files.commit_of(harness, f"{merge}^1", self.env) if new else None
            if new is None or old is None:
                raise Refused(
                    f"origin of {self.harness_repo} has no merge commit {merge[:12]} of the change, or its parent"
                )
            return new, old
        head = await files.commit_of(harness, change["head_sha"], self.env) if change.get("head_sha") else None
        old = await files.merge_base(harness, tip, head, self.env) if head else None
        if head is None or old is None:
            raise Refused(f"origin of {self.harness_repo} has neither the change's merge commit nor its head")
        return head, old

    async def lay_out(self, arm: str, plan: dict, *, ref=None, name=None) -> tuple[Path, Path, str | None]:
        """A session's directory as a replay arm's (``ReplayRun.lay_out``), and on claude-code the repo's
        .claude/settings.json and .claude/hooks/ as the arm's commit has them under .claude/ of the directory, where the
        agent's runtime loads them."""
        root, repository, harness_folder = await super().lay_out(arm, plan, ref=ref, name=name)
        if self.run.runtime == probes.CLAUDE_CODE:
            pairs = [
                (path, path)
                for path in plan["whole"]
                if path == probes.SETTINGS_FILE or path.startswith(probes.HOOKS_DIR + "/")
            ]
            try:
                await files.write_at(plan["harness"], ref or plan["refs"][arm], pairs, root, self.env)
            except (gitops.GitError, OSError) as exc:
                raise RunFailed(
                    f"{name or arm}: the settings and hooks were not placed: {exc}", cause="checkout"
                ) from None
        return root, repository, harness_folder

    async def session(self, cls, arm: str, index: int, plan: dict) -> dict:
        """One session: its directory, its agent, the trace of its work, then the probe's verify on it; what it found."""
        run, name = self.run, f"{arm}-{index}"
        root, repository, harness_folder = await self.lay_out(arm, plan, ref=plan["refs"][arm], name=name)
        start = await files.commit_of(repository, "HEAD", self.env)
        run.worktree = root
        budget = self.arm_budget(2 * self.k)
        if budget is not None:
            run.spec["budget"] = budget
        run.watchdog.spent_usd = 0.0  # a new session, whose cost starts at nothing
        figures = SessionFigures()
        run.listeners.append(figures)
        prompt = probes.probe_prompt(self.probe["task"], folder=repository.name, harness_folder=harness_folder)
        started, error, usage = run.loop.time(), None, None
        try:
            await self.conversation.turns(cls, prompt, None)
            usage = run.outcome.usage if run.outcome else None
        except RunFailed as exc:
            error, usage = exc.error, exc.fields.get("usage")
            run.note(f"Session {index} of arm {arm}: the agent's turn did not end ({exc.error}); its work is checked.")
        finally:
            run.listeners.remove(figures)
        duration_ms = int((run.loop.time() - started) * 1000)
        self.usages.append(usage)
        trace = await self.trace(name, repository, start, figures)
        code = await self.verify_session(arm, index, repository, trace)
        return {
            "arm": arm,
            "index": index,
            "passed": code == 0,
            "exit_code": code,
            "cost_usd": _cost(usage),
            "turns": figures.turns,
            "duration_ms": duration_ms,
            "runtime": run.runtime,
            "model": figures.model or run.spec.get("model"),
            "agent_error": cut(error, probes.MAX_REASON_CHARS) if error else None,
            "session_id": self.conversation.session_id,
        }

    async def trace(self, name: str, repository: Path, start: str | None, figures: SessionFigures) -> Path:
        """The trace file of a session, written once its agent's turn is over, outside its directory and read-only:
        the commands the agent ran and the files of the repository it changed."""
        changed = await files.changed_since(repository, start, self.env) if start else []
        folder = self.directory.path / TRACES
        folder.mkdir(mode=0o700, exist_ok=True)
        path = folder / f"{name}.json"
        body = {"commands": figures.commands, "files": changed[: probes.MAX_FILES]}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o444)
        return path

    async def verify_session(self, arm: str, index: int, repository: Path, trace: Path) -> int:
        """Run the probe's verify on the session's work and its trace, as code the worker does not trust: its exit
        code. The event says the code alone: the verify and its output are the probe's answer key."""
        run = self.run
        run.check()
        started = run.loop.time()
        code, _ = await verify.run_untrusted(
            run, self.probe["verify"], repository, extra_env={probes.TRACE_ENV: str(trace)}
        )
        duration_ms = int((run.loop.time() - started) * 1000)
        text = f"Session {index} of arm {arm}: the probe's verify exited {code} in {duration_ms / 1000:.1f}s."
        run.event("system", {"text": text, "arm": arm, "index": index, "exit_code": code, "duration_ms": duration_ms})
        return code

    async def post(self, body: dict) -> None:
        run = self.run
        try:
            await run.daemon.hub.probe(run.id, body, run.probe_key)
        except HubProblem as exc:
            raise RunFailed(f"the hub did not take the probe's result: {exc}") from None
