"""``evo-agents worker browser``: ``install`` puts Chromium on this worker for the runs dispatched with browser; inside such
a run, ``login`` asks the person who dispatched it to sign in and ``state`` writes the browser's cookies to a file."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from evo_agents.hub import browser_login
from evo_agents.hub.runs import MAX_BROWSERS, RUN_KINDS
from evo_agents.worker.browser import install, logins, session
from evo_agents.worker.home import WorkerHome, WorkerStateError


def register_browser(wsub) -> None:
    from evo_agents.worker.cli import worker_command  # the cli imports this module

    browser = wsub.add_parser(
        "browser", help="the Chromium of the runs dispatched with --browser, which the daemon starts for them"
    )
    commands = browser.add_subparsers(dest="browser_command", required=True)
    installer = commands.add_parser(
        "install",
        help="download Chromium into this user's cache (no sudo) and check it starts here; again, it changes nothing "
        "(restart the daemon so its heartbeat says the feature browser)",
    )
    installer.add_argument(
        "--max",
        type=browsers,
        metavar="N",
        help=f"how many browsers this worker holds at once, 1 to {MAX_BROWSERS} (default: one per core but one and "
        f"per {install.GIB_PER_BROWSER:g} GiB of memory available, as the install measures)",
    )
    installer.set_defaults(func=worker_command(cmd_install))
    login = commands.add_parser(
        "login",
        help="with NAME: sign a browser identity in on this machine, in a Chromium window, no run; inside a run with a "
        "browser, with --identity --url --reason: ask the person who dispatched it to sign in, then end your turn "
        "(the answer Done saves the profile, and a run of one step fails with the identity needs_login)",
    )
    login.add_argument("name", nargs="?", metavar="NAME", help="the identity to sign in on this machine (made if new)")
    login.add_argument("--project", metavar="NAME", help="with NAME: the project of the identity")
    login.add_argument("--owner", choices=("member", "project", "agent"), default="member", help="with NAME: whose")
    login.add_argument("--agent", metavar="HANDLE", help="with NAME and --owner agent: the agent profile's handle")
    login.add_argument(
        "--domain", action="append", default=[], metavar="HOST", help="with NAME: a site it keeps; needed for a new one"
    )
    login.add_argument("--identity", metavar="NAME", help="in a run: the name or id of a browser identity of the run")
    login.add_argument("--url", metavar="URL", help="the page to sign in at (http or https)")
    login.add_argument("--reason", metavar="TEXT", help="in a run: why it needs the session, in a sentence")
    login.set_defaults(func=worker_command(cmd_login))
    state = commands.add_parser(
        "state",
        help="inside a run with a browser: write the browser's cookies as a Playwright storageState file in the run's "
        "own directory (a path in a worktree is refused)",
    )
    state.add_argument(
        "--out", required=True, metavar="FILE", help="a file in runs/<run>/, relative to it, or absolute"
    )
    state.set_defaults(func=worker_command(cmd_state))


def browsers(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= MAX_BROWSERS:
        raise argparse.ArgumentTypeError(f"takes 1 to {MAX_BROWSERS}, not {text}")
    return value


def cmd_install(args) -> int:
    home = WorkerHome()
    installed, changed = install.install(home, os.environ, max_browsers=args.max)
    sandbox = "its own sandbox" if installed.sandbox else "--no-sandbox"
    print(
        f"Chromium {installed.version} at {installed.executable}: {install.DISPLAY}, {sandbox}, "
        f"{installed.max} browser(s) at once."
    )
    if changed:
        print("Restart the worker's daemon so its heartbeat says the feature browser.")
    else:
        print("Already installed: nothing changed.")
    return 0


def _browser_run(command: str):
    from evo_agents.worker.cli import agent_run  # the cli imports this module

    return agent_run(command, tuple(kind for kind in RUN_KINDS if kind != browser_login.KIND))


def cmd_local_login(args) -> int:
    from evo_agents.worker.browser import local_login

    if args.identity or args.reason:
        raise WorkerStateError("NAME signs in on this machine; --identity and --reason belong to a run's login")
    if not args.project:
        raise WorkerStateError("--project NAME says the project of the identity")
    if (args.owner == "agent") != (args.agent is not None):
        raise WorkerStateError("--owner agent goes with --agent HANDLE, and --agent with --owner agent")
    ask: dict = {
        "project": args.project,
        "name": args.name,
        "owner": args.owner,
        "domains": [domain.strip().lower() for domain in args.domain],
    }
    if args.agent:
        ask["agent"] = args.agent
    if args.url:
        if not browser_login.valid_url(args.url):
            raise WorkerStateError("--url is an http or https address with a host")
        ask["login_url"] = args.url
    answer = asyncio.run(local_login.login(WorkerHome(), ask))
    print(f"Saved version {answer.get('version')} of browser identity {args.name}: its runs start logged in.")
    return 0


def cmd_login(args) -> int:
    from evo_agents.worker.cli import with_hub

    if getattr(args, "name", None):
        return cmd_local_login(args)
    if not (args.identity and args.url and args.reason):
        raise WorkerStateError("give NAME (sign in here) or --identity, --url and --reason (ask from inside a run)")

    agent = _browser_run("login")
    if not browser_login.valid_url(args.url):
        raise WorkerStateError("--url is an http or https address with a host")
    body = {"identity": args.identity, "url": args.url, "reason": " ".join(args.reason.split())}

    async def work(hub) -> dict:
        return await hub.browser_login(agent.run_id, body)

    answer = with_hub(agent, work)
    decision_id = answer.get("decision_id")
    if answer.get("outcome") == "failed" or not isinstance(decision_id, int):
        print(f"The run cannot wait for a login and ends: {answer.get('message')}")
        return 0
    identity = answer.get("identity_id")
    logins.note(agent.home, agent.run_id, decision_id, identity if isinstance(identity, int) else 0, args.url)
    with open(agent.home.decisions_path(agent.run_id), "a", encoding="utf-8") as handle:  # the daemon waits on it
        handle.write(json.dumps({"id": decision_id}) + "\n")
    print(f"Login decision #{decision_id} is open: the person who dispatched this run was asked to sign in.")
    print(
        "End your turn now: the run waits with its browser open. The answer comes back as a message naming decision "
        f"#{decision_id}: Done means the session is in the browser (reload the page), Skip means go on without it."
    )
    return 0


def cmd_state(args) -> int:
    agent = _browser_run("state")
    cdp = os.environ.get(session.VARIABLE)
    if not cdp:
        raise WorkerStateError(f"{session.VARIABLE} is not set: this run's agent has no browser to read")
    from evo_agents.worker.browser import state

    path = state.out_path(agent.home, agent.run_id, args.out)
    cookies = asyncio.run(state.read_cookies(cdp))
    state.write(path, state.storage_state(cookies))
    print(f"Wrote the browser's state ({len(cookies)} cookies) to {path}. It holds secrets: keep it out of every repo.")
    return 0
