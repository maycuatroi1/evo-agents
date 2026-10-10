"""The turns of a run's agent in one session, and the waits for its owner between them: a plan run and an author run,
and the turns of a review run's and a judge run's agent.

After each turn, the owner's messages that came in the meantime start the next turn in the same session. A turn
that ends with a decision of the run open (a plan run), or any turn (an author run, whose agent talks with its owner
in the run's chat), moves the run to ``waiting``: the daemon keeps it, with its slot and lease, and the time it waits
does not count toward its timeout. The owner's answer comes through the inbox and goes to a new turn of the agent in
the same session (``running`` again). A run the hub parks (no answer within a day) stops at the end of its turn,
keeps its session and worktrees, and frees its slot; the run that resumes it, claimed with ``resume_of_run_id``, goes
on in them (``Conversation.adopt``).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker import gitops
from evo_agents.worker.runner.common import Parked, ReportRefused, RunFailed, Stopped, first_of, log, now
from evo_agents.worker.runner.transitions import answer_note, waits_for_owner

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run
    from evo_agents.worker.runner.directory import RunDirectory

# What a plan run's agent is told when the owner wrote while its turn was over: an answer to a decision, say.
ANSWER_PROMPT = (
    "The owner of this plan run wrote while your turn was over; a message that answers a decision names it. Go on "
    "with the plan in this session from where you stopped: read the plan as the hub holds it now with "
    "`evo-agents worker plan` first."
)
# And when a run parked while it waited for its owner goes on, as a new run, in the same session and worktrees.
RESUME_PROMPT = (
    "This plan run was parked while it waited for its owner, and goes on now as run #{id}, in the same session and "
    "the same worktrees; EVO_RUN_ID names the new run, and the commands of `evo-agents worker` use it. Go on with the "
    "plan from where you stopped: read the plan as the hub holds it now with `evo-agents worker plan` first."
)


def read_asked(home, run_id: int) -> set[int]:
    """The decisions the agent of plan run ``run_id`` asked, as ``evo-agents worker ask`` noted them."""
    asked: set[int] = set()
    with contextlib.suppress(OSError):
        for line in home.decisions_path(run_id).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                decision_id = json.loads(line).get("id")
                if isinstance(decision_id, int):
                    asked.add(decision_id)
    return asked


class Conversation:
    """The session of the run's agent across its turns (``session_id``), the parked run it goes on from
    (``resume_of``), and what starts each next turn: ``after_turn``, the decisions the run waits for unless the kind
    brings its own, and the prompt it starts with, ``reply_prompt`` then the owner's messages."""

    def __init__(
        self,
        run: Run,
        directory: RunDirectory,
        *,
        reply_prompt: str = ANSWER_PROMPT,
        resume_prompt: str = RESUME_PROMPT,
        after_turn: Callable[[], Awaitable[list[dict]]] | None = None,
    ):
        self.run = run
        self.directory = directory
        spec = run.spec
        self.resume_of = int(spec["resume_of_run_id"]) if spec.get("resume_of_run_id") else None
        self.session_id: str | None = spec.get("session_id") or None
        self.reply_prompt = reply_prompt
        self.resume_prompt = resume_prompt
        self.after_turn = after_turn or self.wait_for_decisions
        run.record["resume_of_run_id"] = self.resume_of
        run.daemon.home.save_run(run.record)

    def adopt(self) -> None:
        """Take over the directory, worktrees, session and decisions of the parked run this run resumes; its record no
        longer names them, so the cleanup of that run leaves them alone."""
        run, old_id = self.run, self.resume_of
        home = run.daemon.home
        record = home.load_run(old_id)
        resumable = record is not None and record.get("kind") in runs.RESUME_KINDS
        directory = Path(record["dir"]) if resumable and record.get("dir") else None
        if directory is None or not directory.is_dir():
            run.note(
                f"The worktrees of parked run #{old_id} are not on this worker any more: the run makes new ones from "
                "the branches on origin, and goes on in the session of the parked run."
            )
            return
        for item in record.get("repos") or []:
            try:
                workspace = gitops.Workspace.from_record(item)
            except ValueError:
                continue
            if workspace.worktree.is_dir():
                self.directory.workspaces[workspace.repo] = workspace
        self.directory.path = directory
        self.session_id = self.session_id or record.get("session_id")
        run.inbox.answered |= {item for item in record.get("answered") or [] if isinstance(item, int)}
        still_open = sorted(read_asked(home, old_id) - run.inbox.answered)
        if still_open:
            lines = "".join(json.dumps({"id": decision_id}) + "\n" for decision_id in still_open)
            with open(home.decisions_path(run.id), "a", encoding="utf-8") as handle:
                handle.write(lines)
        self._adopt_pushes(old_id)
        record.update({"resumed_by": run.id, "dir": None, "repos": [], "moved_to": str(directory)})
        home.save_run(record)
        names = ", ".join(self.directory.workspaces) or "no repo"
        run.note(f"Goes on from parked run #{old_id} in {directory}, with its worktrees of {names}.")

    def _adopt_pushes(self, old_id: int) -> None:
        """What the parked run pushed to a default branch is never put back either."""
        home = self.run.daemon.home
        with contextlib.suppress(OSError):
            pushes = home.pushes_path(old_id).read_text(encoding="utf-8")
            if pushes.strip():
                home.pushes_path(self.run.id).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with open(home.pushes_path(self.run.id), "a", encoding="utf-8") as handle:
                    handle.write(pushes if pushes.endswith("\n") else pushes + "\n")

    async def first_turn(self, prompt: str) -> tuple[str, str | None]:
        """The prompt and session of the agent's first turn: ``prompt`` in a new session, or, for a run that resumes a
        parked one, the owner's messages in that run's session."""
        run = self.run
        if self.resume_of is None:
            return prompt, None
        messages = await run.inbox.unread()
        said = run.inbox.take(messages) if messages else ""
        if self.session_id is None:  # the parked run never told its session: start again, with the messages
            run.note("The parked run left no session to go on with: the agent starts a new one.")
            return runs.clip(prompt + ("\n\n" + said if said else ""), runs.MAX_PROMPT_BYTES), None
        resumed = self.resume_prompt.format(id=run.id) + ("\n\n" + said if said else "")
        return runs.clip(resumed, runs.MAX_PROMPT_BYTES), self.session_id

    async def turns(self, cls, prompt: str, session_id: str | None) -> None:
        """Turns of the agent until one ends with nothing to wait for: after each, the owner's messages that came in
        the meantime, or the answer the run waits for, start the next in the same session."""
        run = self.run
        terminal_first = run.mode == "interactive"
        while True:
            run.outcome, session_id = await run.agent.turns(cls, prompt, session_id, terminal_first=terminal_first)
            terminal_first = False
            self.session_id = session_id or self.session_id
            run.record["session_id"] = self.session_id
            if run.park_asked.is_set():
                raise Parked()
            conflict = run.daemon.home.load_conflict(run.id)
            if conflict is not None:  # `evo-agents worker step` pushed a repo's commits to the side branch
                raise RunFailed(conflict["error"], cause="push_conflict", usage=run.outcome.usage)
            if not run.outcome.completed:
                raise RunFailed(
                    run.outcome.error or f"{run.runtime} ended before its turn completed",
                    usage=run.outcome.usage,
                    cap=run.outcome.cap,
                )
            messages = await self.after_turn()
            if not messages:
                return
            prompt = runs.clip(self.reply_prompt + "\n\n" + run.inbox.take(messages), runs.MAX_PROMPT_BYTES)

    def open_asked(self) -> set[int]:
        """The decisions the agent asked whose answers it has not had."""
        return read_asked(self.run.daemon.home, self.run.id) - self.run.inbox.answered

    async def ensure_running(self) -> None:
        """Report running unless the hub has it so already (the report spawned when the agent started may still be
        on its way), so the next move starts from it."""
        run = self.run
        if run.state in ("leased", "waiting"):
            await run.reports.report("running", only_from=("leased", "waiting"), session_id=self.session_id)

    async def wait_for_decisions(self) -> list[dict]:
        """What starts the agent's next turn: the owner's messages it has not had, at once; else, while a decision of
        the run is open, the messages that come once the owner answers (the run waits). None when nothing is left
        to wait for."""
        run = self.run
        unread = await run.inbox.unread()
        if unread:
            run.note(f"{len(unread)} message(s) of the owner came while the agent worked: a new turn takes them.")
            return unread
        asked = self.open_asked()
        if not waits_for_owner(asked, run.open_decisions):
            return []
        await self.ensure_running()
        try:
            await run.reports.report("waiting", only_from=("running",))
        except ReportRefused as exc:
            if exc.status != 409:
                raise
            run.note(f"The agent's turn ended, and the hub has no decision of the run to wait for ({exc}).")
            return []
        if run.state != "waiting":
            return []
        shown = ", ".join(f"#{decision_id}" for decision_id in sorted(asked)) or "of the run"
        run.note(
            f"The agent's turn ended with decision {shown} open: the run waits for its owner's answer, and the time it "
            "waits does not count toward its timeout."
        )
        return await self.wait_for_answer()

    async def wait_for_answer(self) -> list[dict]:
        """Wait for the owner's messages, the answer to a decision among them, without the agent: the worker keeps the
        run and the heartbeat its lease. Stopped on a cancel or when the hub lets go of the run, Parked when it parks
        it, and nothing once the owner ended the chat of an author run. The time spent here moves the run's deadline
        back."""
        run = self.run
        started = run.loop.time()
        try:
            while True:
                if run.stop_reason is not None:
                    raise Stopped(run.stop_reason)
                if run.park_asked.is_set():
                    raise Parked()
                if run.finish_asked.is_set():
                    return []
                run.inbox_arrived.clear()
                messages = await run.inbox.unread()
                if messages:
                    run.note(answer_note(messages, self.session_id))
                    return messages
                await first_of(
                    (run.inbox_arrived, run.stop_event, run.park_asked, run.finish_asked), run.daemon.heartbeat_s
                )
        finally:
            run.deadline += run.loop.time() - started

    async def park(self) -> None:
        """The hub parked the run: its slot is free; the session and the worktrees stay for the run that resumes
        it, and the hub hears nothing more of this one but its events."""
        run = self.run
        await run.agent.interrupt()
        run.parked = True
        run.state = "parked"
        run.record.update({"state": "parked", "parked_at": now().isoformat(), "session_id": self.session_id})
        run.note(
            f"The hub parked the run: the agent's turn is over, and its session {self.session_id or '(none)'} and the "
            f"worktrees in {self.directory.path} stay on this worker for the run that resumes it."
        )
        run.ended = True
        log.info("run parked", extra={"run_id": run.id, "session_id": self.session_id})
