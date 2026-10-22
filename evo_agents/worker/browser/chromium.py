"""How the daemon starts Chromium (plan agent-browser, criterion a3): the binary itself, never Playwright's launch,
so none of Playwright's default switches come along, --enable-automation among them; with the same flags every time,
so a profile one start wrote opens again at the next (``flags``).

- --headless=new, no screen and no Xvfb (spike decision 4), on the full binary, not the headless shell (decision 11).
- --user-data-dir in the run's directory, made 0700 (decision 9), with Chromium's password manager turned off in its
  Preferences (``quiet_passwords``), and the password store basic, the macOS keychain mocked.
- --remote-debugging-port=0: Chromium picks a free port, which it binds on 127.0.0.1 alone and writes to
  DevToolsActivePort in the profile (``port``), with the path of the browser's websocket.
- --disable-blink-features=AutomationControlled, so navigator.webdriver is not true (decision 2), and the user agent
  of Chrome of the same major version, since --headless=new says HeadlessChrome in it (decision 3).
- --window-size=1280,943, which gives a viewport of 1280x800 at --headless=new (decision 7).
- --no-sandbox only where ``evo-agents worker browser install`` found that Chromium cannot make its own sandbox here
  (no unprivileged user namespaces, decision 1).

Chromium starts in a session of its own, its process group led by it, so that the daemon stops it and every helper it
started at once (``evo_agents.worker.orphans``); its output goes to a log file next to the profile."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

PORT_FILE = "DevToolsActivePort"
PREFERENCES = "Default/Preferences"
WINDOW = "1280,943"
DIR_MODE = 0o700
# the platform part of the user agent of Chrome on each system the worker runs on
PLATFORMS = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "linux": "X11; Linux x86_64"}
USER_AGENT = "Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
TERM_GRACE = 5.0  # seconds a Chromium that was sent SIGTERM has to end before SIGKILL


def user_agent(version: str) -> str:
    """The user agent of Chrome ``version`` on this system."""
    system = "darwin" if sys.platform == "darwin" else "linux"
    return USER_AGENT.format(platform=PLATFORMS[system], major=version.split(".")[0])


def flags(profile: Path, version: str, *, sandbox: bool) -> list[str]:
    """The flags of every start of Chromium ``version`` on ``profile``, the same each time."""
    found = [
        "--headless=new",
        f"--user-data-dir={profile}",
        "--remote-debugging-port=0",
        "--password-store=basic",
        "--use-mock-keychain",
        "--no-first-run",
        "--no-default-browser-check",
        f"--window-size={WINDOW}",
        "--disable-blink-features=AutomationControlled",
        f"--user-agent={user_agent(version)}",
    ]
    if not sandbox:
        found.append("--no-sandbox")
    return [*found, "about:blank"]


def environment(base: Mapping[str, str], libraries: str | None) -> dict[str, str]:
    """``base`` with the libraries the install unpacked for Chromium first on LD_LIBRARY_PATH (Linux; None for none)."""
    env = dict(base)
    if libraries:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(item for item in (libraries, env.get("LD_LIBRARY_PATH")) if item)
    return env


def quiet_passwords(profile: Path) -> None:
    """Turn off Chromium's password manager in the Preferences of ``profile``, keeping what else they hold."""
    path = profile / PREFERENCES
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    held: dict = {}
    with contextlib.suppress(OSError, ValueError):
        found = json.loads(path.read_text(encoding="utf-8"))
        held = found if isinstance(found, dict) else {}
    held["credentials_enable_service"] = False
    section = held.get("profile") if isinstance(held.get("profile"), dict) else {}
    held["profile"] = {**section, "password_manager_enabled": False}
    path.write_text(json.dumps(held), encoding="utf-8")


def start(
    executable: Path | str,
    profile: Path,
    version: str,
    *,
    sandbox: bool,
    libraries: str | None,
    base_env: Mapping[str, str],
    log_path: Path,
) -> subprocess.Popen:
    """Start Chromium on ``profile`` (made 0700, its password manager off, the port file of a start before gone), in
    a session of its own, its output appended to ``log_path``."""
    profile.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(profile, DIR_MODE)
    quiet_passwords(profile)
    (profile / PORT_FILE).unlink(missing_ok=True)
    with open(log_path, "ab") as log:
        return subprocess.Popen(
            [str(executable), *flags(profile, version, sandbox=sandbox)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment(base_env, libraries),
            start_new_session=True,
        )


def port(profile: Path) -> tuple[int, str] | None:
    """(the DevTools port, the path of the browser's websocket) Chromium wrote to the profile once it listens; None
    before."""
    try:
        lines = (profile / PORT_FILE).read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].startswith("/devtools/browser/"):
        return None
    return int(lines[0]), lines[1]


def kill(process: subprocess.Popen, grace: float = TERM_GRACE) -> None:
    """Stop the process group ``process`` leads: SIGTERM, then SIGKILL after ``grace`` seconds; and reap it."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(OSError):
            os.killpg(process.pid, sig)
        try:
            process.wait(grace)
            return
        except subprocess.TimeoutExpired:
            continue


def tail_text(text: str, limit: int = 600) -> str:
    """The end of ``text`` on one line."""
    text = " ".join(text.split())
    return text if len(text) <= limit else "..." + text[-limit:]


def tail(path: Path, limit: int = 600) -> str:
    """The end of the log at ``path`` on one line, empty when there is none."""
    try:
        return tail_text(path.read_text(encoding="utf-8", errors="replace"), limit)
    except OSError:
        return ""
