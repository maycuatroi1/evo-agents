"""The agent of a run: started headless through its runtime's adapter, or handed to a person in its terminal UI
(``terminal``), turn after turn in one session, until its last turn is over.

Each time an agent starts, headless or in its terminal UI, the daemon notes its process group in
``runs/<run>/agent.json`` (``orphans``), and removes the file just before the run counts as ended here, so a daemon
that starts after this one died finds the agents it left. What the runtime leaves out of the agent's environment
because of the run's leases (Claude Code drops the daemon's ANTHROPIC_API_KEY next to a leased
CLAUDE_CODE_OAUTH_TOKEN) is noted once a run (``note_environment``). On Claude Code a run of the Curator uses the Claude
subscription login alone (plan decision 14): ANTHROPIC_API_KEY is left out of its agent's environment, and a machine
that has no subscription login for it fails the run before the agent starts, saying so (``Adapter.login_refusal``).
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from evo_agents.hub import curator, runs
from evo_agents.worker import interactive, orphans
from evo_agents.worker.adapter import AgentEvent, Outcome, RunContext
from evo_agents.worker.runner.common import RunFailed, log
from evo_agents.worker.runner.terminal import Terminal
from evo_agents.worker.runner.transitions import running_from, why_stopped

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

API_KEY = "ANTHROPIC_API_KEY"  # left out of the agent of a run of the Curator on Claude Code (plan decision 14)
NOTE_GROUP_LOOKS = 20  # events after its start at which an adapter is asked again for its agent's process group
STOP_GRACE = 30.0  # seconds an interrupted agent has to end its events
# What the agent is told when a person hands its session back.
HANDBACK_PROMPT = (
    "The owner of this run drove this session in a terminal and has handed it back to you. Go on with the task of "
    "the run from where the session and the working tree stand now, and finish it as the first message of this "
    f"session asks, writing {runs.RESULT_FILE} at the end."
)


class Agent:
    """The agent of a run and what the heartbeat asks of it: a takeover hands its session to a person in tmux
    (``terminal``), a handback lets a new adapter go on headless in the same session."""

    def __init__(self, run: Run):
        self.run = run
        self.adapter = None
        self.running = False
        self.group_noted = False  # agent.json names the process group of the agent that runs now
        self.session_reported: str | None = None
        self.phase = "prepare"  # prepare, headless, interactive (a person drives the agent), after
        self.takeover_asked = False
        self.handback_asked = asyncio.Event()
        self.environment_noted: set[str] = set()  # the lines of environment_notes written already
        self.terminal = Terminal(run, self)

    # The runtime

    def runtime_class(self):
        """The adapter class of the run's runtime; RunFailed when this worker has none."""
        cls = self.run.daemon.adapters.get(self.run.runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.run.runtime}", cause="runtime")
        return cls

    def check_interactive(self, cls) -> None:
        """RunFailed when the run is interactive and its agent cannot be handed to a person here."""
        if self.run.mode != "interactive":
            return
        why = self.interactive_unsupported(cls)
        if why is not None:
            raise RunFailed(
                f"this worker cannot run {self.run.runtime} interactive ({why}): run it headless", cause="runtime"
            )

    def interactive_unsupported(self, cls=None) -> str | None:
        """Why this run's agent cannot be handed to a person in a terminal here, or None when it can."""
        run = self.run
        cls = cls or run.daemon.adapters.get(run.runtime)
        if cls is None or not getattr(cls, "interactive", False):
            return f"its {run.runtime} adapter cannot hand the session to a terminal"
        if not run.daemon.tmux.available:
            return "tmux is not on PATH"
        if self.terminal.failed:
            return f"the terminal UI did not open earlier: {self.terminal.failed}"
        return None

    # Control from the heartbeat

    def request_takeover(self) -> None:
        """The owner asked to drive the agent in a terminal; the heartbeat says so until the run is interactive."""
        run = self.run
        if run.ended or self.takeover_asked or self.phase in ("interactive", "after"):
            return
        why = self.interactive_unsupported()
        if why is not None:
            run.unsupported("takeover", why)
            return
        self.takeover_asked = True
        name = interactive.session_name(run.id)
        log.info("takeover asked", extra={"run_id": run.id, "phase": self.phase})
        if self.phase == "headless":
            run.note(
                "The owner asked for a takeover: the agent ends its turn, then its session opens in its terminal UI "
                f"in tmux session {name}."
            )
            if self.running and self.adapter is not None:
                run.spawn(self.stop_at_boundary(self.adapter))
        else:
            run.note(f"The owner asked for a takeover: the agent starts in its terminal UI in tmux session {name}.")

    def request_handback(self) -> None:
        """The owner asked to let the agent go on headless; the heartbeat says so until the run is running."""
        if self.phase == "interactive" and not self.handback_asked.is_set():
            log.info("handback asked", extra={"run_id": self.run.id})
            self.handback_asked.set()

    async def stop_at_boundary(self, adapter) -> None:
        try:
            await asyncio.wait_for(adapter.stop_at_turn_boundary(), STOP_GRACE)
        except Exception:
            log.warning("the agent was not asked to end its turn", extra={"run_id": self.run.id}, exc_info=True)

    async def interrupt(self) -> None:
        if self.adapter is None or not self.running:
            return
        try:
            await asyncio.wait_for(self.adapter.interrupt(), STOP_GRACE)
        except Exception:
            log.warning("interrupting the agent failed", extra={"run_id": self.run.id}, exc_info=True)

    # The environment

    def env(self) -> dict[str, str]:
        """The environment of the run's agent, and of the commands the daemon runs for the run."""
        run = self.run
        env = dict(run.daemon.env)
        env.update(
            {
                "EVO_RUN_ID": str(run.id),
                "EVO_RUN_KIND": str(run.spec.get("kind") or "step"),
                "EVO_WORKER_HOME": str(run.daemon.home.root),  # the commands of `evo-agents worker` read the run here
                "EVO_RUN_PROJECT": str(run.project),
                "EVO_RUN_PLAN": str(run.spec.get("plan_id") or ""),
                "EVO_RUN_STEP": str(run.spec.get("step_key") or ""),
            }
        )
        env.update(run.credentials.agent_vars)  # env leases, and git's configuration for the leased origins
        if self.subscription_only:  # plan decision 14: the Claude subscription login alone
            env.pop(API_KEY, None)
        return env

    @property
    def subscription_only(self) -> bool:
        """Whether this run's agent is a run of the Curator on Claude Code, which uses the Claude subscription login
        alone, never ANTHROPIC_API_KEY (plan decision 14)."""
        return self.run.curator is not None and self.run.runtime == "claude-code"

    async def check_login(self, cls, context: RunContext) -> None:
        """Fail the run before its agent starts when the runtime may not start it with this machine's login: a run of
        the Curator on Claude Code needs a Claude subscription login (``Adapter.login_refusal``)."""
        if not self.subscription_only:
            return
        run = self.run
        has_key = API_KEY in run.daemon.env or API_KEY in run.credentials.agent_vars
        if has_key:
            note = (
                f"{API_KEY} is left out of the agent's environment: a run of the Curator on Claude Code uses the "
                "Claude subscription login alone (plan decision 14)."
            )
            if note not in self.environment_noted:
                self.environment_noted.add(note)
                run.note(note)
        why = await asyncio.to_thread(cls.login_refusal, context)
        if why:
            alone = f"; this machine has {API_KEY} alone, which it does not use" if has_key else ""
            raise RunFailed(why + alone, cause="runtime")

    def context(self, prompt: str, session_id: str | None) -> RunContext:
        """What the adapter of an agent that starts now gets."""
        run = self.run
        return RunContext(
            run=dict(run.spec),
            worktree=run.worktree,
            prompt=prompt,
            env=self.env(),
            resume_session=session_id,
            leased=run.credentials.withheld,
            spent_usd=run.watchdog.spent_usd,
            spent_seconds=run.watchdog.spent_before + run.agent_seconds(),
        )

    def note_environment(self, cls, context: RunContext) -> None:
        """What the runtime leaves out of the agent's environment because of the run's leases
        (``Adapter.environment_notes``), each line once a run."""
        for line in cls.environment_notes(context):
            if line not in self.environment_noted:
                self.environment_noted.add(line)
                self.run.note(line)

    # The turns

    async def turns(
        self, cls, prompt: str, session_id: str | None, *, terminal_first: bool
    ) -> tuple[Outcome, str | None]:
        """Run the agent on ``prompt`` (in the session ``session_id``, or a new one) until its turn is over: headless,
        and in its terminal UI while a person drives it (``terminal_first``, or a takeover), each phase going on with
        the session of the one before. How the last headless phase ended, and the session's id."""
        first_prompt = prompt
        in_terminal = terminal_first or self.takeover_asked
        try:
            while True:
                if in_terminal:
                    session_id = await self.terminal.drive(cls, session_id, prompt)
                    in_terminal = False
                    prompt = HANDBACK_PROMPT if session_id else first_prompt
                    continue
                outcome = await self.headless(cls, prompt, session_id)
                session_id = self.adapter.session_id or session_id
                self.run.check()
                if self.takeover_asked and session_id and self.interactive_unsupported(cls) is None:
                    in_terminal = True
                    continue
                return outcome, session_id
        finally:
            self.phase = "after"

    async def headless(self, cls, prompt: str, session_id: str | None) -> Outcome:
        """The agent headless, on a new session or going on with ``session_id``, until its events end."""
        run = self.run
        context = self.context(prompt, session_id)
        await self.check_login(cls, context)
        adapter = cls(context)
        self.adapter = adapter
        self.phase = "headless"
        run.inbox.waits = False
        try:
            await adapter.start()
        except Exception as exc:
            log.warning("agent did not start", extra={"run_id": run.id}, exc_info=True)
            raise RunFailed(f"{run.runtime} did not start: {type(exc).__name__}: {exc}") from None
        self.running = True
        run.agent_starts()
        pid = self.group_of(adapter)
        await self.note_agent(pid)
        self.group_noted = pid is not None
        run.inbox.ack_pending()  # the messages its prompt carries are the agent's now
        if session_id:
            run.note(f"{run.runtime} goes on headless in session {session_id}.")
        else:
            run.note(f"{run.runtime} started in {run.worktree}.")
        self.note_environment(cls, context)
        run.spawn(run.reports.report_running(running_from(run.state)))
        if self.takeover_asked or run.park_requested():  # asked while the agent was starting
            run.spawn(self.stop_at_boundary(adapter))
        try:
            outcome = await self.consume(adapter)
        finally:
            self.running = False
        # Claude Code's total is the session's running total, which the next agent in the session starts from.
        run.watchdog.spent_usd = max(run.watchdog.spent_usd, curator.run_cost(outcome.usage))
        return outcome

    def group_of(self, adapter) -> int | None:
        try:
            pid = adapter.group_pid()
        except Exception:  # an adapter's bug must not stop the run
            log.warning(
                "the adapter did not name the agent's process group", extra={"run_id": self.run.id}, exc_info=True
            )
            return None
        return pid if isinstance(pid, int) and pid > 1 else None

    async def note_agent(self, pid: int | None) -> None:
        """Note the agent that runs now in runs/<run>/agent.json (``orphans``): the process group ``pid`` leads, or
        none (an agent in its terminal UI, whose tmux session a daemon that starts closes, or an adapter that does not
        name its group). The file stays until the run ends here."""
        run = self.run
        agent = await asyncio.to_thread(orphans.describe, pid)
        agent.update({"runtime": run.runtime, "phase": self.phase})
        try:
            run.daemon.home.save_agent(run.id, agent)
        except OSError as exc:
            log.warning("agent.json not written", extra={"run_id": run.id, "error": str(exc)})

    async def consume(self, adapter) -> Outcome:
        """The agent's events into the run's log until they end, or until the run is asked to stop or runs out of
        time, which interrupts the agent; how the agent ended."""
        run = self.run
        pump_task = asyncio.create_task(self._pump(adapter))
        stop_task = asyncio.create_task(run.stop_event.wait())
        try:
            done, _ = await asyncio.wait(
                {pump_task, stop_task},
                timeout=max(0.0, run.deadline - run.loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stop_task.cancel()
        if pump_task not in done:
            if run.stop_reason is None:
                run.stop_reason = "timeout"
            run.note(f"Stopping the agent: {why_stopped(run.stop_reason)}.")
            await self.interrupt()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(pump_task), STOP_GRACE)
            if not pump_task.done():
                pump_task.cancel()
        await asyncio.wait({pump_task})  # done, or done cancelling; raises nothing of the task's
        if not pump_task.cancelled() and pump_task.exception() is not None:
            log.warning("the adapter's events failed", extra={"run_id": run.id}, exc_info=pump_task.exception())
        try:
            return await asyncio.wait_for(adapter.wait(), STOP_GRACE)
        except TimeoutError:
            return Outcome(False, f"{run.runtime} did not end within {STOP_GRACE:g}s of being stopped")
        except Exception as exc:
            return Outcome(False, f"{run.runtime} failed: {type(exc).__name__}: {exc}")

    async def _pump(self, adapter) -> None:
        run = self.run
        looks = 0
        async for item in adapter.events():
            if isinstance(item, AgentEvent):
                run.event(item.kind, item.body, item.at)
            if not self.group_noted and looks < NOTE_GROUP_LOOKS:  # a runtime that started after start returned
                looks += 1
                pid = self.group_of(adapter)
                if pid is not None:
                    await self.note_agent(pid)
                    self.group_noted = True
            session_id = adapter.session_id
            if session_id and session_id != self.session_reported and run.state == "running":
                self.session_reported = session_id
                run.spawn(run.reports.report_running(("running",)))
