"""A question run (kind ``question``, ``QuestionRun``, ``evo_agents.hub.question_runs``): one headless turn of a core
plugin's agent type on a question a person outside the hub asked, which reads only and answers in text.

The daemon takes the run's leases for the repos of its project it has a checkout of (the others it leaves out, saying
so, as a task run does), checks it can read each, and makes the directory ``~/.evo/worker/worktrees/<project>-<run>``
with a worktree of each, detached at the commit origin's default branch has, as an author run does
(``RunDirectory.prepare_detached``). The agent works in EVO_RUN_KIND ``question``, one turn that asks nobody anything
(Claude Code's question tools are off) and reads the hub as a reader. When it ends, the daemon reads the answer the
agent wrote in .evo-run/result.json under ``"answer"`` (``question_runs.answer_text``, cut to
``question_runs.MAX_ANSWER_CHARS``) and reports ``done`` with it (StateReport.answer); without one the run fails with
the cause ``no_answer``. Nothing is committed or pushed (``gitops.check_push`` refuses a question run), no pull request
is opened, no diff is uploaded, and what the agent left in the worktrees goes with them at the cleanup of old runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import question_runs, runs
from evo_agents.worker.runner import access
from evo_agents.worker.runner.common import RunFailed
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory
from evo_agents.worker.runner.transitions import summary_of
from evo_agents.worker.runner.verify import RESULT_MAX_BYTES

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


async def _no_more_turns() -> list[dict]:
    """A question run is one turn: nobody writes to its agent."""
    return []


class QuestionRun:
    """A question run: a worktree of each repo of its project this worker has a checkout of, detached at origin's
    default branch, one headless turn of the agent on the question, and its answer sent with the report of done;
    nothing committed or pushed."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"question run of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory, after_turn=_no_more_turns)
        self.left_out: list[str] = []  # repos of the run this worker has no checkout of
        self.no_origin: list[str] = []  # repos of the run the project lists no origin for

    async def steps(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: a question run of project {run.project}, "
            f"{run.runtime}, timeout {run.timeout_s // 60} min of agent time. It reads only: nothing is committed or "
            "pushed, and its answer goes to the hub."
        )
        cls = run.agent.runtime_class()
        if run.mode == "interactive":
            raise RunFailed("a question run runs headless only")
        names = [entry.get("repo") for entry in repos]
        await access.take_credentials(run, names, best_effort=True)  # cloning those it lacks first
        run.check()
        held = self.held_repos(repos)
        await access.check(run, [entry["repo"] for entry in held], path=access.run_path(run))
        run.check()
        await self.directory.prepare_detached(held)
        run.check()
        await self.conversation.turns(cls, self.prompt(), None)
        answer, summary = self.read_answer()
        run.summary = summary
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        await run.reports.end("done", answer=answer, summary=summary, usage=run.outcome.usage if run.outcome else None)

    def held_repos(self, repos: list[dict]) -> list[dict]:
        """The repos of the run this worker has a checkout of and the project lists an origin for; the others are
        noted and left out (``access.leaves_out``)."""
        held = []
        for entry in repos:
            name = entry.get("repo")
            why = access.leaves_out(self.run, name) if isinstance(name, str) and name else "checkout"
            if why is None:
                held.append(entry)
            else:
                (self.no_origin if why == "origin" else self.left_out).append(str(name))
        access.left_out_notes(self.run, self.left_out, self.no_origin)
        return held

    def read_answer(self) -> tuple[str, str | None]:
        """The answer the agent wrote in .evo-run/result.json, cut to its limit, and its summary when it wrote one;
        RunFailed with the cause no_answer without one."""
        run = self.run
        path = self.directory.path / runs.RESULT_FILE
        if path.is_symlink() or not path.is_file():
            raise RunFailed(f"the agent wrote no {runs.RESULT_FILE}: the question has no answer", cause="no_answer")
        try:
            if path.stat().st_size > RESULT_MAX_BYTES:
                raise ValueError(f"it is over {RESULT_MAX_BYTES} bytes")
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RunFailed(
                f"{runs.RESULT_FILE} was not read ({exc}): the question has no answer", cause="no_answer"
            ) from None
        data = data if isinstance(data, dict) else {}
        said = data.get("answer")
        answer = question_runs.answer_text(said)
        if answer is None:
            raise RunFailed(f'the agent wrote no "answer" in {runs.RESULT_FILE}', cause="no_answer")
        if len(said.strip()) > len(answer):
            run.note(f"The answer has {len(said.strip())} characters: it is cut to {question_runs.MAX_ANSWER_CHARS}.")
        run.note(f"The agent answered in {len(answer)} characters.")
        return answer, summary_of(data)

    def prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the repos
        this worker has no checkout of."""
        prompt = self.run.spec.get("prompt") or ""
        moved = self.directory.moved()
        extra = ["", "Worktree folders that are not named as their repo:", *moved] if moved else []
        extra += access.left_out_lines(self.left_out, self.no_origin)
        return runs.clip(prompt + "\n".join(extra) + ("\n" if extra else ""), runs.MAX_PROMPT_BYTES)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        """A question run changes nothing: no diff is uploaded."""
        return b""

    async def release_branch(self) -> None:
        await self.directory.release()
