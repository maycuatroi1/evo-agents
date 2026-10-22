"""``evo-agents worker browser install``: Chromium for the runs dispatched with browser (``install``)."""

from __future__ import annotations

import argparse
import os

from evo_agents.hub.runs import MAX_BROWSERS
from evo_agents.worker.browser import install
from evo_agents.worker.home import WorkerHome


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
