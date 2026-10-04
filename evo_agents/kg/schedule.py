"""``evo-agents kg schedule``: a macOS LaunchAgent that runs ``kg sync --due --all --build`` every hour.

launchd starts a job with almost no environment, so the plist names this evo-agents by absolute path,
declares PATH explicitly, pins EVO_KG_HOME, and sets ``EVO_KG_TRIGGER=schedule`` so a run can tell it was
started by the schedule. Output of every run is appended to ``<kg home>/schedule.log``. When this machine is
signed in to a hub (``evo-agents hub login``), the command also gets ``--push``: every run pushes the runs the
hub lacks. Install again after signing in or out to change that.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from evo_agents.kg.corpus import kg_home

LABEL = "io.github.maycuatroi1.evo-agents.kg-sync"
INTERVAL = 3600
SYNC_ARGS = ("kg", "sync", "--due", "--all", "--build")
PUSH_ARG = "--push"
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


class ScheduleError(RuntimeError):
    pass


def executable(argv0: str | None = None) -> list[str]:
    """How launchd should start the evo-agents that is running now: the console script by absolute path,
    or this interpreter with ``-m evo_agents`` when started as ``python -m evo_agents``."""
    argv0 = sys.argv[0] if argv0 is None else argv0
    if argv0 and Path(argv0).stem == "evo-agents":
        found = argv0 if os.sep in argv0 else shutil.which(argv0)
        if found:
            return [os.path.abspath(found)]
    return [os.path.abspath(sys.executable), "-m", "evo_agents"]


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def signed_in() -> bool:
    """Whether this machine holds hub credentials, so the scheduled sync can push."""
    from evo_agents.hub.client import HubError, load_credentials

    try:
        load_credentials()
    except HubError:
        return False
    return True


def sync_args(push: bool | None = None) -> tuple[str, ...]:
    """The kg command the schedule runs; with ``--push`` when ``push``, or, when it is None, when signed in."""
    push = signed_in() if push is None else push
    return (*SYNC_ARGS, PUSH_ARG) if push else SYNC_ARGS


def build_plist(argv0: str | None = None, home: Path | None = None, push: bool | None = None) -> dict:
    home = home or kg_home()
    log = str(home / "schedule.log")
    return {
        "Label": LABEL,
        "ProgramArguments": [*executable(argv0), *sync_args(push)],
        "StartInterval": INTERVAL,
        "EnvironmentVariables": {
            "PATH": os.pathsep.join([str(Path.home() / ".local" / "bin"), *SYSTEM_PATH]),
            "EVO_KG_HOME": str(home),
            "EVO_KG_TRIGGER": "schedule",
        },
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def render(plist: dict) -> str:
    return plistlib.dumps(plist).decode("utf-8")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _require_launchd() -> None:
    if sys.platform != "darwin":
        raise ScheduleError(
            "kg schedule install needs launchd (macOS). Elsewhere, run this every hour from cron or a systemd"
            f" timer: {shlex.join([*executable(), *sync_args()])}"
        )


def _loaded(run) -> bool:
    return run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True).returncode == 0


def _launchctl(run, *args: str) -> None:
    proc = run(["launchctl", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ScheduleError(f"launchctl {' '.join(args)} failed with exit {proc.returncode}: {detail}")


def install(argv0: str | None = None, run=None) -> Path:
    """Write the plist and load it with ``launchctl bootstrap``, booting out a copy already loaded."""
    _require_launchd()
    run = run or subprocess.run
    plist = build_plist(argv0)
    Path(plist["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)  # launchd will not create it
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if _loaded(run):
        _launchctl(run, "bootout", f"{_domain()}/{LABEL}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(render(plist), encoding="utf-8")
    os.replace(tmp, path)
    _launchctl(run, "bootstrap", _domain(), str(path))
    return path


def uninstall(run=None) -> bool:
    """``launchctl bootout`` the job if it is loaded, then delete the plist. True if there was a plist."""
    _require_launchd()
    run = run or subprocess.run
    if _loaded(run):
        _launchctl(run, "bootout", f"{_domain()}/{LABEL}")
    path = plist_path()
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed
