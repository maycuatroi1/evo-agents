"""A review run (kind ``review``, ``ReviewRun``): the Curator's Reviewer of one night of a project.

Its worktrees are detached at the commit origin's default branch has (``RunDirectory.prepare_detached``), the worker
counts the open items of reports and the learned skills waiting for review in them (``figures``) into
``.evo-run/worktree-figures.json``, the agent records findings and proposals with ``evo-agents worker
finding|propose``, and the run ends done with the agent's summary: nothing is committed or pushed
(``gitops.check_push`` refuses a review run), and the run's GitHub token reads only. A review run asks no decision and
never parks: a park means the hub no longer holds it for this worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker import figures
from evo_agents.worker.runner import access, verify
from evo_agents.worker.runner.common import RunFailed, log
from evo_agents.worker.runner.conversation import Conversation
from evo_agents.worker.runner.directory import RunDirectory

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

WORKTREE_FIGURES = "worktree-figures.json"  # in .evo-run/ of a review run's directory


class ReviewRun:
    """A review run: a worktree of each of its repos, detached at the commit origin's default branch has, the agent
    reading in their directory and writing findings and proposals through ``evo-agents worker finding|propose``, and
    nothing committed or pushed at its end."""

    parks = False
    chat = False

    def __init__(self, run: Run):
        self.run = run
        run.title = run.spec.get("title") or f"review of {run.spec.get('project')}"
        self.directory = RunDirectory(run)
        self.conversation = Conversation(run, self.directory)

    async def steps(self) -> None:
        run, spec = self.run, self.run.spec
        repos = [entry for entry in spec.get("repos") or [] if isinstance(entry, dict)]
        names = ", ".join(str(entry.get("repo")) for entry in repos) or "no repo"
        run.note(
            f"Run #{run.id} claimed by worker {run.daemon.config.name}: the review of project {run.project} over "
            f"{names}, {run.runtime}, {run.mode}, timeout {run.timeout_s // 60} min of agent time. It reads only: "
            "nothing is committed or pushed."
        )
        cls = run.agent.runtime_class()
        run.agent.check_interactive(cls)
        if not repos:
            raise RunFailed("the review run names no repo to read")
        names = [entry.get("repo") for entry in repos]
        await access.take_credentials(run, names)
        run.check()
        await access.check(run, names, path=access.run_path(run))
        run.check()
        await self.directory.prepare_detached(repos)
        run.check()
        await self.count_figures()
        await self.conversation.turns(cls, self.prompt(), None)
        run.summary = verify.read_summary(run, self.directory.path)
        await self.conversation.ensure_running()
        await run.reports.report("verifying")
        run.check()
        await run.reports.end("done", summary=run.summary, usage=run.outcome.usage if run.outcome else None)

    async def count_figures(self) -> None:
        """Count what lives in the worktrees' files (``figures.scan``) into .evo-run/worktree-figures.json, and say
        how much in the run's log."""
        run = self.run
        worktrees = {name: workspace.worktree for name, workspace in self.directory.workspaces.items()}
        try:
            found = await asyncio.to_thread(figures.scan, worktrees)
        except Exception:
            log.exception("the worktrees' figures were not counted", extra={"run_id": run.id})
            return
        path = self.directory.path / runs.RESULT_DIR / WORKTREE_FIGURES
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
        counts = {key: len(value) for key, value in found.items()}
        run.note(
            f"Counted in the worktrees: {counts['reports']} open items of reports, {counts['learned_skills']} learned "
            f"skills waiting for review ({runs.RESULT_DIR}/{WORKTREE_FIGURES}).",
            worktree_figures=counts,
        )

    def prompt(self) -> str:
        """The run's prompt, with where each repo's worktree is when a folder is not named as its repo, and the file
        of the worktrees' figures."""
        prompt = self.run.spec.get("prompt") or ""
        moved = self.directory.moved()
        extra = [
            "",
            f"The worker counted the open items of reports and the learned skills waiting for review in the worktrees: "
            f"{runs.RESULT_DIR}/{WORKTREE_FIGURES}.",
        ]
        if moved:
            extra += ["Worktree folders that are not named as their repo:", *moved]
        return runs.clip(prompt + "\n".join(extra) + "\n", runs.MAX_PROMPT_BYTES)

    # What the parts every kind shares ask of it

    def watched(self) -> list[tuple[str, Path, str]]:
        return self.directory.watched()

    async def diff(self) -> bytes:
        """A review run changes nothing: no diff is uploaded."""
        return b""

    async def release_branch(self) -> None:
        await self.directory.release()
