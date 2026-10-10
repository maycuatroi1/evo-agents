"""The agent's session in its runtime's terminal UI, while a person drives it.

After a takeover, or from the start in interactive mode, a person drives the agent's session in its runtime's
terminal UI in tmux session evo-run-N (``interactive``), and the run is reported interactive; a handback, or the
person leaving the UI, lets a new adapter go on headless in the same session. The run's log meanwhile reads the
runtime's record of the session, or what the terminal prints. The worker's end of the run's web terminal connects
once the run is interactive, where the worker's config allows it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from evo_agents.worker import interactive
from evo_agents.worker.runner.common import RunFailed, cut, log

if TYPE_CHECKING:
    from evo_agents.worker.runner.agent import Agent
    from evo_agents.worker.runner.context import Run

TERMINAL_POLL = 1.0  # seconds between looks at the tmux session while a person drives the agent
TERMINAL_LOG_GRACE = 5.0  # seconds the log of the terminal has, once its session is closed, to read the last records


class Terminal:
    """The terminal UI of a run's agent (``tui``) while a person drives it, and the worker's end of the run's web
    terminal (``web``)."""

    def __init__(self, run: Run, agent: Agent):
        self.run = run
        self.agent = agent
        self.tui = None  # the runtime's terminal UI while a person drives the agent
        self.web: interactive.WebTerminal | None = None
        self.wanted = False  # a browser asked for the terminal before the run was interactive
        self.failed: str | None = None  # why a terminal UI did not open; later takeovers are refused

    # The web terminal

    def open_web(self) -> None:
        """A browser waits for the run's terminal: connect the worker's end once the run is interactive, or say in
        the terminal that this worker does not allow it."""
        run = self.run
        if run.ended:
            return
        if self.agent.phase != "interactive" or self.tui is None or run.state != "interactive":
            self.wanted = True
            return
        self.wanted = False
        config = run.daemon.config
        if self.web is None:
            self.web = interactive.WebTerminal(
                run.id,
                run.daemon.tmux,
                allowed=config.allow_web_terminal,
                worker=config.name,
                config_path=run.daemon.home.config_path,
            )
        if not self.web.connected and not self.web.allowed and "web terminal" not in run.unsupported_noted:
            run.unsupported_noted.add("web terminal")
            run.note(
                "The owner opened the web terminal; this worker does not allow it (allow_web_terminal is off in its "
                "config.json), so the terminal says so and closes."
            )
        self.web.open(run.daemon.hub.session, run.daemon.hub.url, run.daemon.token)

    async def close_web(self) -> None:
        if self.web is not None:
            await self.web.close()

    # A person drives the agent

    async def drive(self, cls, session_id: str | None, prompt: str) -> str | None:
        """Hand the agent's session (a new one on ``prompt`` when ``session_id`` is None) to a person: the runtime's
        terminal UI in tmux session evo-run-N, the run reported interactive, until a handback or the person leaves the
        UI. The session's id, for the agent that goes on headless."""
        run, agent = self.run, self.agent
        agent.phase = "interactive"
        agent.takeover_asked = False
        agent.handback_asked.clear()
        name = interactive.session_name(run.id)
        context = agent.context(prompt, session_id)
        await agent.check_login(cls, context)
        tui = cls.tui(context, session_id)
        try:
            await self._start(tui, name)
        except Exception as exc:
            return await self._not_opened(tui, name, exc, session_id)
        self.tui = tui
        agent.note_environment(cls, context)
        run.agent_starts()
        await agent.note_agent(None)
        agent.group_noted = False
        logs = asyncio.create_task(self.follow_log(tui, name))  # before anyone is told the session is there
        try:
            how = await self._hand_over(tui, name)
        finally:
            await self._close(tui, name, logs)
        session_id = tui.session_id or session_id
        then = f"{run.runtime} goes on headless" + (f" in session {session_id}" if session_id else "")
        if how == "handback":
            run.note(f"The owner handed the run back: the terminal UI was closed, and {then}.")
        else:
            run.note(f"The terminal UI ended: {then}.")
        return session_id

    async def _start(self, tui, name: str) -> None:
        """The terminal UI in its tmux session."""
        run = self.run
        for line in await tui.prepare():
            run.note(line)
        command = tui.command(name)
        await run.daemon.tmux.start(
            name,
            command,
            tui.environment(),
            cwd=run.worktree,
            scratch=run.daemon.home.run_dir(run.id),
            drop=tui.drop_env,
            withheld=run.credentials.withheld,
            env_command=run.credentials.pane_command(),
        )

    async def _not_opened(self, tui, name: str, exc: Exception, session_id: str | None) -> str | None:
        """The terminal UI did not open: the agent goes on headless, and later takeovers are refused; RunFailed for a
        run interactive from the start, whose agent never started."""
        run = self.run
        with contextlib.suppress(Exception):
            await tui.close()
        with contextlib.suppress(Exception):
            await run.daemon.tmux.kill(name)
        reason = cut(f"{type(exc).__name__}: {exc}", 300)
        log.warning("the terminal UI did not open", extra={"run_id": run.id, "error": reason})
        self.failed = reason
        if run.mode == "interactive" and not run.agent_started:
            raise RunFailed(f"the terminal UI of {run.runtime} did not open: {reason}") from None
        run.note(f"The terminal UI of {run.runtime} did not open ({reason}); the agent goes on headless.")
        return session_id

    async def _hand_over(self, tui, name: str) -> str:
        """Report the run interactive, say where the session is, and wait while a person drives the agent."""
        run = self.run
        run.record["tmux_session"] = name
        socket = await run.daemon.tmux.socket_path(name)
        if socket:
            run.record["tmux_socket"] = socket
        with contextlib.suppress(OSError):
            run.daemon.home.save_run(run.record)
        await run.reports.report("interactive", only_from=("leased", "running"), session_id=tui.session_id)
        self.agent.session_reported = tui.session_id
        shown = f", session {tui.session_id}" if tui.session_id else ""
        run.note(
            f"{run.runtime} runs in its terminal UI in tmux session {name}{shown}: `evo-agents worker attach "
            f"{run.id}` on {run.daemon.config.name} opens it, and so does the run's Terminal tab on the web."
        )
        if self.wanted:
            self.open_web()
        return await self.wait(tui, name)

    async def _close(self, tui, name: str, logs: asyncio.Task) -> None:
        """Close the web terminal, the tmux session and the terminal UI, and let the log read what came last."""
        await self.close_web()
        await self.run.daemon.tmux.kill(name)
        await tui.close()  # sets logs_done: the log reads what came last, then ends
        try:
            await asyncio.wait_for(asyncio.shield(logs), TERMINAL_LOG_GRACE)
        except TimeoutError:
            logs.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await logs
        self.tui = None

    async def wait(self, tui, name: str) -> str:
        """Wait while a person drives the agent: ``handback`` or ``exit`` (the UI ended); Stopped on a cancel, the
        run's timeout or the hub letting go of the run."""
        run, agent = self.run, self.agent
        tmux = run.daemon.tmux
        while True:
            run.check()
            if agent.handback_asked.is_set():
                return "handback"
            if not await tmux.alive(name):
                return "exit"
            if tui.session_id and tui.session_id != agent.session_reported:  # a new session's id, found late
                agent.session_reported = tui.session_id
                run.spawn(run.reports.report_session("interactive", ("interactive",), tui.session_id))
            waits = {asyncio.create_task(agent.handback_asked.wait()), asyncio.create_task(run.stop_event.wait())}
            try:
                await asyncio.wait(
                    waits,
                    timeout=max(0.0, min(TERMINAL_POLL, run.deadline - run.loop.time())),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waits:
                    task.cancel()

    async def follow_log(self, tui, name: str) -> None:
        """The run's log while a person drives the agent: the runtime's record of the session, or what the terminal
        prints when the runtime keeps none this worker reads."""
        run = self.run
        source = tui.logs()
        pane_log = None
        try:
            if source is None:
                pane_log = run.daemon.home.run_dir(run.id) / "terminal.log"
                await run.daemon.tmux.pipe(name, pane_log)
                run.note(
                    "The terminal UI keeps no record this worker reads: the log shows the text its terminal prints."
                )
                source = interactive.follow_pane(pane_log, tui.logs_done)
            async for event in source:
                run.event(event.kind, event.body, event.at)
        except Exception:
            log.warning("the log of the terminal failed", extra={"run_id": run.id}, exc_info=True)
        finally:
            if pane_log is not None:
                pane_log.unlink(missing_ok=True)
