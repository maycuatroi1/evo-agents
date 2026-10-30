"""The agent of a run: started headless through its runtime's adapter, or handed to a person in its terminal UI
(``terminal``), turn after turn in one session, until its last turn is over.

Each time an agent starts, headless or in its terminal UI, the daemon notes its process group in
``runs/<run>/agent.json`` (``orphans``), and removes the file just before the run counts as ended here, so a daemon
that starts after this one died finds the agents it left. What the runtime leaves out of the agent's environment
because of the run's leases (Claude Code drops the daemon's ANTHROPIC_API_KEY next to a leased
CLAUDE_CODE_OAUTH_TOKEN) is noted once a run (``note_environment``). On Claude Code a run of the Curator uses the Claude
subscription login alone (plan decision 14): ANTHROPIC_API_KEY is left out of its agent's environment, and a machine
that has no subscription login for it fails the run before the agent starts, saying so (``Adapter.login_refusal``).
The agent of a run on Claude Code gets the auto-compact window of ``claude_code.compact_window`` in its environment.
One under a profile that is not its project's default (the claim's ``own_home``) gets a Claude Code config dir of its
own in the run's directory (``claude_home``), written before each agent starts, and the run fails with cause
credentials before the agent starts when this machine has no login to give it (``prepare_home``).
The agent of a chat run, and of a run under a profile that is not its project's default, gets GIT_AUTHOR_NAME and
GIT_AUTHOR_EMAIL of its profile, so the commits it makes itself name it as their author (``authorship``).
A run under an agent profile that limits tool, skill or model, or has instructions, fails before any of its work when
the runtime's adapter cannot apply the profile (``Adapter.profile_refusal``), or when the adapter finds so as it starts
the agent (``ProfileRefused``), with cause ``runtime``; such a run is never handed to a person in a terminal UI, which
applies no profile.
A headless agent whose account hits its usage limit is stopped, and goes on in the same session with another account
when the hub moves the run to one (``limits``); else the run fails with cause rate_limit. One whose account failed to
authenticate goes on the same way, else fails with cause account_auth.
A run whose leases hold one of kind gateway runs its model through the hub's gateway route (``runtimes.gateway``): on
Claude Code its agent gets none of ``gateway.CLAUDE_DROPPED`` of the daemon's environment, and on this machine a
config dir of its own without a login (``claude_home``), whatever its profile.

A run of a plugin's runtime (``<plugin>/<name>``) starts its agent through the adapter of ACP made from the declaration
its claim carries (``acp.adapter_class``), and fails with cause credentials before the agent starts when a variable the
declaration names is set neither by the run's leases nor in the daemon's environment (``Adapter.environment_refusal``).

The agent runs where the run's environment says (``environment``, from the claim's ``environment``,
``evo_agents.worker.environments``): on the host, or in a container of a plugin's environment, which the run's log
says once and agent.json names, so the next daemon removes what is left of its containers after one that died. A run
in a container is never handed to a person in a terminal UI, which runs on the host.

The agent packs the run's profile attaches (the claim's ``packs``) are built into ``runs/<run>/packs/`` before its
first agent starts (``build_packs``, ``evo_agents.worker.packs``), and every agent of the run gets them
(``RunContext.packs``). A pack that does not build (its bundle does not download, is not the one the hub recorded, or
is of a plugin the owner did not accept), or that holds what the runtime does not load (``Adapter.pack_refusal``),
fails the run with cause runtime before its agent starts, saying why. A run with packs is never handed to a person in
a terminal UI, which loads none.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.hub import browser_login, curator, runs
from evo_agents.hub.credentials import HUB_TOKEN_ENV, HUB_URL_ENV
from evo_agents.plugins.runtimes import AcpRuntime, is_plugin_runtime
from evo_agents.worker import autonomy_hook, environments, interactive, orphans, packs, venvs
from evo_agents.worker.adapter import AgentEvent, Outcome, ProfileRefused, RunContext
from evo_agents.worker.browser import logins as browser_logins
from evo_agents.worker.browser import session as browser_session
from evo_agents.worker.credentials import config_env, credential_asks
from evo_agents.worker.runner import authorship
from evo_agents.worker.runner.common import RunFailed, log
from evo_agents.worker.runner.limits import Limits
from evo_agents.worker.runner.terminal import Terminal
from evo_agents.worker.runner.transitions import running_from, why_stopped
from evo_agents.worker.runtimes import claude_home, gateway, hub_tools, profiles
from evo_agents.worker.runtimes.acp import adapter_class
from evo_agents.worker.runtimes.claude_code import compact_window, leased_oauth
from evo_agents.worker.runtimes.common import with_first_request

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
        self.limits = Limits(run)  # the usage limit of the agent's account, and the move to another one
        self._environment = None  # where the agent runs (``environment``), read from the claim once
        self._packs: tuple | None = None  # the agent packs of the run's profile, built before its first agent
        self.browser: browser_session.BrowserSession | None = None  # the run's Chromium, for a run with browser
        self.logins_seen: set[int] = set()  # the login decisions whose answer was handled
        self.credentials_seen: set[int] = set()  # the credential requests whose answer took the leases again
        self._gate: list = []  # the run's hook of autonomy (``autonomy_hook.for_run``), built for its first agent

    # The runtime

    @property
    def environment(self) -> environments.Environment:
        """Where the run's agent runs, from its claim: the host, or a container of a plugin's environment; ValueError
        when the claim names one this worker does not read, which the preflight reports."""
        if self._environment is None:
            run = self.run
            self._environment = environments.for_run(
                run.spec,
                home_dir=run.daemon.home.run_dir(run.id) / environments.HOME_DIR,
                worker=environments.worker_label(run.daemon.home.root),
                base=run.daemon.env,
            )
        return self._environment

    def runtime_class(self):
        """The adapter class of the run's runtime, a plugin's made from the declaration of the claim; RunFailed when
        this worker has none, or when it cannot apply the run's agent profile (``check_profile``)."""
        runtime = self.run.runtime
        cls = self.plugin_class() if is_plugin_runtime(runtime) else self.run.daemon.adapters.get(runtime)
        if cls is None:
            raise RunFailed(f"this worker has no adapter for {self.run.runtime}", cause="runtime")
        self.check_profile(cls)
        return cls

    def plugin_class(self):
        """The adapter of ACP of the run's plugin runtime, from the declaration its claim carries; RunFailed when the
        claim carries none this worker reads, or one of another runtime."""
        runtime = self.run.runtime
        try:
            declaration = AcpRuntime.from_json(self.run.spec.get("plugin_runtime"))
        except ValueError as exc:
            raise RunFailed(
                f"the claim carries no declaration of {runtime} this worker reads: {exc}", cause="runtime"
            ) from None
        if declaration.runtime != runtime:
            raise RunFailed(f"the claim declares {declaration.runtime}, not the run's {runtime}", cause="runtime")
        return adapter_class(declaration)

    def check_profile(self, cls) -> None:
        """Say in the run's log what its agent profile limits in the session; RunFailed when the adapter cannot apply
        it (``Adapter.profile_refusal``). Nothing for a run whose profile changes nothing."""
        run = self.run
        profile = profiles.Profile.of(run.spec)
        if profile is None or not profile.applies:
            return
        why = cls.profile_refusal(run.spec)
        if why:
            run.note(f"Agent profile: {why}; the agent did not start.", cause="runtime")
            raise RunFailed(f"agent profile: {why}", cause="runtime")
        run.note(profiles.summary(profile))

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
        found = run.spec.get("environment")
        if found is not None:
            name = found.get("environment") if isinstance(found, dict) else None
            return f"its agent runs in a container of environment {name or 'of a plugin'}"
        profile = profiles.Profile.of(run.spec)
        if profile is not None and profile.applies:
            return f"agent profile {profile.label} limits the session, and the terminal UI does not apply it"
        if run.spec.get("packs"):
            return "its agent profile attaches agent packs, which the terminal UI does not load"
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
        for key in (HUB_TOKEN_ENV, HUB_URL_ENV):  # the run's token of the hub: from its lease, never the daemon's
            env.pop(key, None)
        env.update(run.credentials.agent_vars)  # env leases, and git's configuration for the leased origins
        route = run.credentials.gateway  # the hub's gateway route: no Claude login, nor key, beside it
        for key in [key for key in env if gateway.drops(route, key)]:
            env.pop(key)
        env.pop(browser_session.VARIABLE, None)  # the endpoint of the run's own browser, and of no other
        if self.browser is not None and self.browser.cdp:
            env[browser_session.VARIABLE] = self.browser.cdp
        if self.subscription_only:  # plan decision 14: the Claude subscription login alone
            env.pop(API_KEY, None)
        if run.runtime == "claude-code":  # its auto-compact window, headless and in the terminal UI
            env.update(compact_window(env))
        env.update(authorship.author_env(run.spec, authorship.hub_url(run)))  # its own commits name the agent
        env.update(self.trailer_config(env))  # and carry the run's trailers
        if claude_home.wanted(run.spec, run.runtime, route is not None) and run.worktree:  # a config dir of its own
            env["CLAUDE_CONFIG_DIR"] = str(claude_home.config_dir(run.worktree))
            env["GH_CONFIG_DIR"] = str(claude_home.gh_dir(run.worktree))  # empty: none of the owner's gh logins
        if run.venvs:  # the .venv/bin of each repo whose environment the worker made, first (runner.venv)
            for key in venvs.FOREIGN:
                env.pop(key, None)
            env["PATH"] = venvs.front(env.get("PATH"), [str(venvs.bin_dir(path)) for path in run.venvs.values()])
        return env

    def trailer_config(self, env: dict[str, str]) -> dict[str, str]:
        """The git configuration that names the run's hooks, which add its trailers Evo-Principal and Evo-Agent to the
        agent's own commits (``authorship.trailer_hooks``), after what ``env`` sets; empty for a run without trailers,
        without a work tree, or in an environment other than this machine's host."""
        run = self.run
        if run.worktree is None or run.spec.get("environment"):
            return {}
        directory = run.daemon.home.run_dir(run.id) / "git-hooks"
        hooks = authorship.trailer_hooks(run.spec, directory, Path(run.worktree))
        return {} if hooks is None else config_env(env, [("core.hooksPath", str(hooks))])

    @property
    def subscription_only(self) -> bool:
        """Whether this run's agent is a run of the Curator on Claude Code, which uses the Claude subscription login
        alone, never ANTHROPIC_API_KEY (plan decision 14)."""
        return self.run.curator is not None and self.run.runtime == "claude-code"

    async def check_login(self, cls, context: RunContext) -> None:
        """Fail the run before its agent starts when the runtime may not start it with this machine's login: a run of
        the Curator on Claude Code needs a Claude subscription login (``Adapter.login_refusal``); one with a Claude
        Code config dir of its own needs a login to give it (``prepare_home``)."""
        await self.prepare_home(context)
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

    async def prepare_home(self, context: RunContext) -> None:
        """Write the Claude Code config dir of the run's agent's own before it starts (``claude_home.prepare``), and
        say once where its login comes from; RunFailed with cause credentials when this machine has no login to give
        it. For a run whose runtime keeps the owner's home, say so once."""
        run = self.run
        if claude_home.keeps_owner_home(run.spec, run.runtime):
            self.note_once(claude_home.OWNER_HOME_NOTE.format(runtime=run.runtime))
        configured = context.env.get("CLAUDE_CONFIG_DIR")
        route = run.credentials.gateway is not None
        if not configured or not claude_home.wanted(run.spec, run.runtime, route):
            return
        directory = Path(configured)
        server = hub_tools.home_server(context, profiles.Profile.of(run.spec))
        try:
            login = await asyncio.to_thread(
                claude_home.prepare,
                directory,
                owner_env=run.daemon.env,
                leased=leased_oauth(context),
                gateway=route,
                server=server,
                lock=Path(run.daemon.home.root) / claude_home.REFRESH_LOCK,
            )
        except claude_home.RefreshFailed as exc:
            why = f"the owner's Claude login expires within the hour and was not refreshed: {exc}"
            run.note(f"{why}; the agent did not start.", cause="credentials")
            raise RunFailed(why, cause="credentials") from None
        except OSError as exc:
            raise RunFailed(f"Claude Code's config dir {directory} was not written: {exc}", cause="runtime") from None
        if login is None:
            run.note(f"{claude_home.REFUSAL}; the agent did not start.", cause="credentials")
            raise RunFailed(claude_home.REFUSAL, cause="credentials")
        self.note_once(claude_home.note(directory, login))

    def note_once(self, line: str) -> None:
        if line not in self.environment_noted:
            self.environment_noted.add(line)
            self.run.note(line)

    async def build_packs(self, cls) -> tuple:
        """The agent packs of the run's profile, built for its runtime once a run (``packs.build``); RunFailed with
        cause runtime, noted in the run's log, when one does not build or the runtime does not load it."""
        if self._packs is not None:
            return self._packs
        run = self.run
        tickets = run.spec.get("packs") or []
        browser = run.spec.get("browser") is True  # its Chromium has an agent pack of its own, whatever the profile
        if not tickets and not browser:
            self._packs = ()
            return self._packs
        accepted = getattr(run.daemon.config, "plugins", None)
        directory = run.daemon.home.run_dir(run.id) / packs.PACKS_DIR
        try:
            built = await asyncio.to_thread(packs.build, tickets, directory, accepted=accepted, browser=browser)
        except packs.PackFailed as exc:
            why = str(exc)
        except OSError as exc:
            why = f"they were not written to {directory}: {exc}"
        else:
            why = cls.pack_refusal(built)
            if why and any(pack.pack == packs.BROWSER_PACK for pack in built):
                why += f"; a run with a browser needs a runtime that loads MCP servers, or {packs.NO_BROWSER_HINT}"
        if why:
            run.note(f"Agent packs: {why}; the agent did not start.", cause="runtime")
            raise RunFailed(f"agent packs: {why}", cause="runtime")
        run.note(packs.summary(built))
        self._packs = built
        return built

    def context(self, prompt: str, session_id: str | None) -> RunContext:
        """What the adapter of an agent that starts now gets."""
        run = self.run
        env = self.env()
        return RunContext(
            run=dict(run.spec),
            worktree=run.worktree,
            prompt=prompt,
            env=env,
            resume_session=session_id,
            leased=run.credentials.withheld,
            spent_usd=run.watchdog.spent_usd,
            spent_seconds=run.watchdog.spent_before + run.agent_seconds(),
            environment=self.environment,
            packs=self.with_browser(self._packs or ()),
            autonomy=self.gate(),
            floor=autonomy_hook.floor_for_run(run, env),
        )

    def gate(self):
        """The run's hook of autonomy, None for a run without a snapshot: built once a run."""
        if not self._gate:
            self._gate.append(autonomy_hook.for_run(self.run))
        return self._gate[0]

    def with_browser(self, built: tuple) -> tuple:
        """``built``, the run's agent packs, with the DevTools endpoint of the run's Chromium as it is now in the
        variables of their MCP servers (``packs.BROWSER_VARIABLE``)."""
        cdp = self.browser.cdp if self.browser is not None else None
        for pack in built:
            if cdp:
                pack.variables[packs.BROWSER_VARIABLE] = cdp
            else:
                pack.variables.pop(packs.BROWSER_VARIABLE, None)
        return built

    def note_environment(self, cls, context: RunContext) -> None:
        """What the runtime leaves out of the agent's environment because of the run's leases
        (``Adapter.environment_notes``), and where the agent runs when not on the host, each line once a run."""
        where = context.environment.note()
        for line in [*([where] if where else []), *cls.environment_notes(context)]:
            if line not in self.environment_noted:
                self.environment_noted.add(line)
                self.run.note(line)

    # The turns

    async def turns(
        self, cls, prompt: str, session_id: str | None, *, terminal_first: bool
    ) -> tuple[Outcome, str | None]:
        """Run the agent on ``prompt`` (in the session ``session_id``, or a new one) until its turn is over: headless,
        and in its terminal UI while a person drives it (``terminal_first``, or a takeover), each phase going on with
        the session of the one before. How the last headless phase ended, with the first request of the first headless
        phase (``with_first_request``; null when the terminal UI, which reports no usage, came first), and the
        session's id."""
        await self.open_browser()
        first_prompt = prompt
        in_terminal = terminal_first or self.takeover_asked
        phases: list[dict | None] = []  # the usage of each headless phase, in order
        unseen = in_terminal  # the run's first request in the session is the terminal UI's
        try:
            while True:
                if in_terminal:
                    session_id = await self.terminal.drive(cls, session_id, prompt)
                    in_terminal = False
                    prompt = HANDBACK_PROMPT if session_id else first_prompt
                    continue
                outcome = await self.headless(cls, prompt, session_id)
                phases.append(outcome.usage)
                outcome = replace(outcome, usage=with_first_request(outcome.usage, phases, unseen=unseen))
                session_id = self.adapter.session_id or session_id
                self.run.check()
                outcome = await self.limits.after(outcome)
                if outcome is None:  # the run moved to another account: a new agent goes on in the session
                    prompt = self.limits.prompt if session_id else first_prompt
                    continue
                if self.takeover_asked and session_id and self.interactive_unsupported(cls) is None:
                    in_terminal = True
                    continue
                return outcome, session_id
        finally:
            self.phase = "after"

    async def open_browser(self) -> None:
        """Start the run's Chromium before its first agent, for a run dispatched with browser (``browser.session``);
        nothing for any other run, or once it runs."""
        run = self.run
        if run.spec.get("browser") is not True or (self.browser is not None and self.browser.cdp):
            return
        identities = run.spec.get("browser_identities") or ()
        self.browser = self.browser or browser_session.BrowserSession(run.daemon, run.id, identities)
        try:
            await self.browser.open()
        finally:
            for line in self.browser.profiles.take_notes():
                run.note(line)
        self.browser.watch(lambda: run.state)
        self.note_once(
            f"Browser: Chromium runs for the run; its agent finds its DevTools in {browser_session.VARIABLE}."
        )

    async def save_browser(self) -> None:
        """Send the profiles of the run's browser identities back to the hub before its final report, while the run
        holds them (``BrowserSession.save``); nothing when the daemon stops at once."""
        browser = self.browser
        if browser is None or self.run.daemon.hard_stop.is_set():
            return
        if getattr(self.run.kind, "saves_browser", True) is False:  # a login session cancelled or out of time
            return
        try:
            await browser.save()
        except Exception:
            log.exception("the run's browser profiles were not saved", extra={"run_id": self.run.id})
        for line in browser.profiles.take_notes():
            self.run.note(line)

    async def login_answered(self, messages: list[dict]) -> None:
        """The owner's messages are about to reach the agent: for each that answers a login decision the agent asked
        (``evo-agents worker browser login``), Done saves the run's browser profile to its identity at once and
        restarts Chromium on the same endpoint (``BrowserSession.checkpoint``), and Skip saves nothing. A save that
        fails is noted; the agent goes on either way."""
        run, browser = self.run, self.browser
        if browser is None or browser.cdp is None:
            return
        asked = browser_logins.read(run.daemon.home, run.id)
        for decision_id, option in browser_logins.chosen(messages, asked):
            if decision_id in self.logins_seen:
                continue
            self.logins_seen.add(decision_id)
            if option != browser_login.LOGIN_DONE:
                run.note(f"Login decision #{decision_id} was answered without Done: no profile is saved.")
                continue
            try:
                saved = await browser.checkpoint()
            except Exception as exc:
                log.exception("the browser profile was not saved at the login", extra={"run_id": run.id})
                run.note(f"Login decision #{decision_id}: Done, but the browser did not restart cleanly ({exc}).")
                continue
            finally:
                for line in browser.profiles.take_notes():
                    run.note(line)
            if saved:
                run.note(f"Login decision #{decision_id}: Done. The profile is saved to the identity.")
            else:
                run.note(f"Login decision #{decision_id}: Done, but the profile was not saved (see above).")

    async def credentials_answered(self, messages: list[dict]) -> None:
        """The owner's messages are about to reach the agent: when one answers a credential request the agent asked
        (``evo-agents worker ask --env-var NAME --hub-account``), the daemon asks for the run's leases again
        (``RunCredentials.swap``), once for those messages, so the value the owner pasted and the run's token of the hub
        are in what ``evo-agents worker env --run N`` prints, and in the environment of the agent's next start."""
        run = self.run
        asked = credential_asks(run.daemon.home, run.id)
        answered = sorted(
            {
                message["decision_id"]
                for message in messages
                if isinstance(message.get("decision_id"), int)
                and message["decision_id"] in asked
                and message["decision_id"] not in self.credentials_seen
            }
        )
        if not answered:
            return
        self.credentials_seen.update(answered)
        shown = ", ".join(f"#{decision_id}" for decision_id in answered)
        if await run.credentials.swap():
            run.note(
                f"Credential request {shown} was answered: the run took its leases again, which its agent reads with "
                f"`evo-agents worker env --run {run.id}`."
            )
        else:
            run.note(
                f"Credential request {shown} was answered, but the hub did not lease the run its credentials again: "
                "what the answer gave reaches the run when it resumes."
            )

    async def close_browser(self) -> None:
        """Close the run's Chromium and remove its directory (``BrowserSession.close``): the run is over here."""
        browser, self.browser = self.browser, None
        if browser is not None:
            await browser.close(0 if self.run.daemon.hard_stop.is_set() else browser_session.STOP_GRACE)

    async def headless(self, cls, prompt: str, session_id: str | None) -> Outcome:
        """The agent headless, on a new session or going on with ``session_id``, until its events end."""
        run = self.run
        await self.build_packs(cls)
        context = self.context(prompt, session_id)
        await self.check_login(cls, context)
        lacking = cls.environment_refusal(context)
        if lacking:
            raise RunFailed(lacking, cause="credentials")
        adapter = cls(context)
        self.adapter = adapter
        self.phase = "headless"
        run.inbox.waits = False
        try:
            await adapter.start()
        except ProfileRefused as exc:
            run.note(f"Agent profile: {exc}; the agent did not start.", cause="runtime")
            raise RunFailed(f"agent profile: {exc}", cause="runtime") from None
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
        environment = self.environment
        if environment.container:  # what the next daemon removes after one that died
            agent["containers"] = {"worker": environment.worker, "run": run.id, "environment": environment.name}
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
                run.agent_event(item)
                self.limits.see(self, item)
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
