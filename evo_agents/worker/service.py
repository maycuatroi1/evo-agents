"""``evo-agents worker service``: keep the worker daemon running in the background.

- macOS: the LaunchAgent ``io.github.maycuatroi1.evo-agents.worker`` in ``~/Library/LaunchAgents``, loaded into the
  ``gui/<uid>`` domain with ``launchctl bootstrap``, as ``evo-agents kg schedule`` loads its own.
- Linux: the systemd user unit ``evo-agents-worker.service`` in ``~/.config/systemd/user``, enabled and started with
  ``systemctl --user``.

Either way the service runs ``evo-agents worker run --quiet``, naming this evo-agents by absolute path, right away and
at each login. A daemon that exits with an error is started again 10 seconds later; one that stops cleanly (exit 0,
after SIGTERM) stays stopped, and so does one that is no longer a worker (revoked on the hub or with ``worker
revoke``), which exits ``home.EXIT_REVOKED``: the systemd unit lists that status in ``RestartPreventExitStatus``, and
since launchd tells exits apart only as 0 or not, the LaunchAgent sets EVO_WORKER_REVOKED_EXIT to 0. A daemon that
updated its install to the tag nightly exits ``home.EXIT_UPDATED`` (75), an error to both managers, so it is started
again 10 seconds later on the new install (``selfupdate``); a daemon checks at its start that its plist or unit does
start it again before it updates itself. A service
manager starts a job with almost no environment, so the service keeps PATH as it was when ``install`` ran, which is
where the daemon looks for claude, opencode, codex, tmux and git, and pins EVO_WORKER_HOME to the state directory.
Install again after a runtime moves to another directory. Installing again keeps every other EVO_WORKER_* variable
the installed plist or unit sets (``EVO_WORKER_OPENCODE_MODEL``, ``EVO_WORKER_CLAUDE_CODE_EFFORT`` and the like, which
the owner may have added by hand), and writes PATH, EVO_WORKER_HOME and EVO_WORKER_REVOKED_EXIT as a first install
does. Stopping the service sends the daemon SIGTERM and kills what is left 60 seconds later. The daemon, and so its
agents, start with umask 077 (``Umask`` 63 in the plist, ``UMask=0077`` in the unit): what a run writes is its
owner's alone. ``install`` then runs ``evo-agents worker doctor`` with the service's PATH and prints its high and
medium findings as warnings (``doctor.warnings``); they never stop the install.

The daemon writes ``worker.log`` itself (``logs``: rotated at 10 MiB, five old files kept). What it prints before that
log is open, such as a missing extra or a machine that is not a worker, goes to ``service.log`` in the state directory
on macOS and to the user journal on Linux.

A machine may hold the nodes of several agents, each with a state directory of its own (``EVO_WORKER_HOME``) and a
service of its own: ``--agent HANDLE`` (or ``EVO_WORKER_AGENT``) names the service ``evo-agents-worker-<handle>.service``
(Linux) or ``<LABEL>.<handle>`` (macOS) and is written into the service's environment. Without it the names are the
fixed ones of the one node of a machine, and nothing changes for it.

On Linux every worker service of the machine runs in the user slice ``evo-agents-workers.slice`` (SLICE), which
``install`` writes beside the unit: their daemons and agents together may take MEMORY_HIGH of the machine's memory
before the kernel reclaims theirs and slows their allocations, never more than MEMORY_MAX, at which it kills their
biggest process (``OOMPolicy=continue`` keeps the daemon), and never swap. A 4-vCPU VPS whose runs ran several
``pytest -n auto`` beside Postgres swapped until every daemon on it went silent for five minutes, and the hub lost
their runs while the agents still worked (#1620, #1629); the rest of the machine, sshd and the user's own services
among them, keeps what the slice leaves. A user's manager gets the memory controller alone on most distributions
(``Delegate=pids memory``), so the slice sets no CPU weight, which it would ignore. Uninstalling one node leaves the
slice, which the other nodes may still run in. Standard library only.
"""

from __future__ import annotations

import functools
import getpass
import importlib.util
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path

from evo_agents.hub.client import write_atomic
from evo_agents.kg.schedule import executable
from evo_agents.worker.home import EXIT_REVOKED, HOME_VARIABLE, REVOKED_EXIT_VARIABLE, WorkerHome, WorkerStateError

LABEL = "io.github.maycuatroi1.evo-agents.worker"
UNIT = "evo-agents-worker.service"
AGENT_VARIABLE = "EVO_WORKER_AGENT"  # the handle of the agent whose node this service runs; unset for the one node
_HANDLE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,38}$")
RUN_ARGS = ("worker", "run", "--quiet")
RESTART_SECONDS = 10
UMASK = 0o077  # files the daemon and its agents create are the user's alone
SLICE = "evo-agents-workers.slice"  # the systemd user slice of every worker service of the machine
MEMORY_HIGH = "80%"  # of the machine's memory: past it the kernel reclaims the slice's memory and slows it
MEMORY_MAX = "90%"  # never more: the kernel kills the slice's biggest process instead of swapping the host
STOP_SECONDS = 60
POLL_SECONDS = 0.5
START_WAIT_SECONDS = 10.0
SETTLE_SECONDS = 2.0  # a daemon still running this long after it started got past reading its configuration
CALL_TIMEOUT = 30
RUNTIME_COMMANDS = ("claude", "opencode", "codex", "tmux", "git")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
FILE_MODE = 0o644
EXIT_FAILED = 1
EXIT_USAGE = 2
SYSTEMD_PROPERTIES = ("LoadState", "ActiveState", "SubState", "MainPID", "ExecMainStatus", "NRestarts")
KEPT_PREFIX = "EVO_WORKER_"  # the variables of the installed service that installing again keeps
WRITTEN = (
    "PATH",
    HOME_VARIABLE,
    REVOKED_EXIT_VARIABLE,
    AGENT_VARIABLE,
)  # what each install writes, whatever the old file said
_PRINT_LINE = re.compile(r"^\t([a-z][a-z ]*?) = (.*)$")


class ServiceError(Exception):
    """The service could not be installed, removed or read; ``str`` says why and what to do."""


def agent_handle(value: str | None) -> str | None:
    """``value`` as the handle of an agent (lower case), None for none; ServiceError when it cannot name a service."""
    if value is None or not value.strip():
        return None
    handle = value.strip().lstrip("@").lower()
    if not _HANDLE.match(handle):
        raise ServiceError(f"{value!r} is not an agent handle: use letters, digits, - and _")
    return handle


def label_of(agent: str | None = None) -> str:
    """The LaunchAgent label: ``LABEL`` for the one node of a machine, ``LABEL.<agent>`` for an agent's node."""
    return f"{LABEL}.{agent}" if agent else LABEL


def unit_of(agent: str | None = None) -> str:
    """The systemd user unit: ``UNIT`` for the one node of a machine, ``evo-agents-worker-<agent>.service``."""
    return f"{UNIT.removesuffix('.service')}-{agent}.service" if agent else UNIT


@dataclass(frozen=True)
class Spec:
    """What the service runs: the command, the PATH it gets, the worker's state directory, and the EVO_WORKER_*
    variables kept from the service installed before."""

    program: tuple[str, ...]
    path: str
    home: Path
    kept: dict[str, str] = field(default_factory=dict)
    agent: str | None = None

    @property
    def output(self) -> Path:
        """Where launchd sends the daemon's stdout and stderr."""
        return self.home / "service.log"

    @property
    def environment(self) -> dict[str, str]:
        agent = {AGENT_VARIABLE: self.agent} if self.agent else {}
        return {"PATH": self.path, HOME_VARIABLE: str(self.home), **agent, **kept_variables(self.kept)}


@dataclass
class Status:
    manager: str
    name: str
    file: str
    installed: bool
    loaded: bool
    running: bool
    state: str | None = None
    pid: int | None = None
    last_exit: str | None = None
    restarts: int | None = None

    def to_json(self) -> dict:
        return asdict(self)


def service_path(path: str | None = None) -> str:
    """``path`` (default: PATH now) without relative entries or repeats, the system's directories added when missing.
    A relative entry would resolve against the service's working directory, not the one install ran in."""
    current = os.environ.get("PATH", "") if path is None else path
    entries: list[str] = []
    for entry in [*current.split(os.pathsep), *SYSTEM_PATH]:
        if not entry or not os.path.isabs(entry):
            continue
        entry = os.path.normpath(entry)
        if entry not in entries:
            entries.append(entry)
    return os.pathsep.join(entries)


def kept_variables(environment: Mapping | None) -> dict[str, str]:
    """The EVO_WORKER_* variables of ``environment`` that installing again keeps: all of them, sorted, except those
    each install writes (WRITTEN)."""
    return {
        key: value
        for key, value in sorted((environment or {}).items())
        if isinstance(key, str) and key.startswith(KEPT_PREFIX) and key not in WRITTEN and isinstance(value, str)
    }


def build_spec(
    home: WorkerHome | None = None,
    argv0: str | None = None,
    path: str | None = None,
    kept: Mapping[str, str] | None = None,
    agent: str | None = None,
) -> Spec:
    """The service of ``home``, of the node of ``agent`` when given; ``kept`` is the environment of the service
    installed before, whose EVO_WORKER_* variables it keeps."""
    home = home or WorkerHome()
    return Spec(
        program=(*executable(argv0), *RUN_ARGS),
        path=service_path(path),
        home=Path(os.path.abspath(home.root)),
        kept=kept_variables(kept),
        agent=agent,
    )


def runtimes_on(path: str) -> dict[str, str | None]:
    """Where each command the daemon may start lies on ``path``, or None."""
    return {name: shutil.which(name, path=path) for name in RUNTIME_COMMANDS}


# launchd


def build_plist(spec: Spec) -> dict:
    # KeepAlive starts again any job that exits other than 0, so a daemon that is no longer a worker exits 0 here.
    environment = {**spec.environment, REVOKED_EXIT_VARIABLE: "0"}
    return {
        "Label": label_of(spec.agent),
        "ProgramArguments": list(spec.program),
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(spec.home),
        "Umask": UMASK,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_SECONDS,
        "ExitTimeOut": STOP_SECONDS,
        "StandardOutPath": str(spec.output),
        "StandardErrorPath": str(spec.output),
    }


def parse_launchctl_print(text: str) -> dict[str, str]:
    """The top-level ``key = value`` lines of ``launchctl print``: state, pid, runs, last exit code and so on."""
    facts: dict[str, str] = {}
    for line in text.splitlines():
        match = _PRINT_LINE.match(line)
        if match and match.group(1) not in facts:
            facts[match.group(1)] = match.group(2).strip()
    return facts


# systemd


def _unit_quote(value: str, *, command: bool = False) -> str:
    """``value`` as one word of a systemd unit line: ``%`` (a specifier) doubled, ``$`` doubled on a command line,
    and in double quotes, with backslash escapes, when it holds a space or a quote."""
    if "\n" in value or "\r" in value:
        raise ServiceError(f"cannot write {value!r} into a systemd unit: it holds a line break")
    word = value.replace("%", "%%")
    if command:
        word = word.replace("$", "$$")
    if word and not re.search(r"[\s\"'\\]", word):
        return word
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_unit(spec: Spec) -> str:
    command = " ".join(_unit_quote(word, command=True) for word in spec.program)
    environment = " ".join(_unit_quote(f"{key}={value}") for key, value in spec.environment.items())
    lines = [
        "# Written by `evo-agents worker service install`; install again to change it.",
        "[Unit]",
        "Description=evo-agents worker"
        + (f" of agent {spec.agent}" if spec.agent else "")
        + ": runs the plan steps a hub hands this machine",
        "Documentation=https://github.com/maycuatroi1/evo-agents/blob/main/docs/workers.md",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={command}",
        f"Environment={environment}",
        f"UMask={UMASK:04o}",
        "Restart=on-failure",
        f"RestartSec={RESTART_SECONDS}",
        # A daemon that is no longer a worker: starting it again changes nothing.
        f"RestartPreventExitStatus={EXIT_REVOKED}",
        # SIGTERM to the daemon alone, which lets its agents end; SIGKILL to all that is left after the timeout.
        "KillMode=mixed",
        f"TimeoutStopSec={STOP_SECONDS}",
        # An OOM kill ends the command the kernel picked, not the daemon with its agents and leases.
        "OOMPolicy=continue",
        # The memory ceiling of every worker service of the machine together (build_slice).
        f"Slice={SLICE}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def build_slice() -> str:
    """The slice of every worker service of the machine: what they take together, so their runs never swap the host
    into a stall that silences the daemons (see the module's docstring)."""
    lines = [
        "# Written by `evo-agents worker service install`; install again to change it.",
        "[Unit]",
        "Description=evo-agents workers: the worker services of this machine and their agents",
        "Documentation=https://github.com/maycuatroi1/evo-agents/blob/main/docs/workers.md",
        "",
        "[Slice]",
        f"MemoryHigh={MEMORY_HIGH}",
        f"MemoryMax={MEMORY_MAX}",
        "MemorySwapMax=0",
        "",
    ]
    return "\n".join(lines)


# The service managers


class Manager:
    """A service manager: ``run`` calls its command line tool (``subprocess.run`` unless a test hands a fake)."""

    name = ""
    tool = ""
    service = ""

    def __init__(self, run=None, sleep=None, agent: str | None = None):
        self._run = run or subprocess.run
        self._sleep = sleep or time.sleep
        self.agent = agent
        self.service = self.name_of(agent)

    @staticmethod
    def name_of(agent: str | None) -> str:
        raise NotImplementedError

    def file(self) -> Path:
        raise NotImplementedError

    def render(self, spec: Spec) -> str:
        raise NotImplementedError

    def start(self, spec: Spec) -> None:
        raise NotImplementedError

    def stop(self) -> bool:
        raise NotImplementedError

    def status(self) -> Status:
        raise NotImplementedError

    def program(self) -> str | None:
        """The command line in the installed file, for people to read."""
        raise NotImplementedError

    def installed_environment(self) -> dict[str, str]:
        """The environment the installed file gives the daemon; empty when no file is installed or it cannot be
        read."""
        raise NotImplementedError

    def installed_home(self) -> Path | None:
        """EVO_WORKER_HOME in the installed file."""
        value = self.installed_environment().get(HOME_VARIABLE)
        return Path(value) if value else None

    def _base_args(self) -> list[str]:
        return [self.tool]

    def _call(self, *args: str, check: bool = True, timeout: float = CALL_TIMEOUT) -> subprocess.CompletedProcess:
        argv = [*self._base_args(), *args]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise ServiceError(
                f"{self.tool} is not on PATH, so {self.name} cannot keep the daemon running; run "
                "`evo-agents worker run` under a supervisor of your own instead"
            ) from None
        except subprocess.TimeoutExpired:
            raise ServiceError(f"{shlex.join(argv)} did not finish within {int(timeout)} seconds") from None
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip() or "no output"
            raise ServiceError(f"{shlex.join(argv)} failed with exit {proc.returncode}: {detail}")
        return proc

    def _write(self, spec: Spec) -> Path:
        path = self.file()
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, self.render(spec).encode("utf-8"), FILE_MODE)
        return path

    def install(self, spec: Spec) -> Status:
        """Write the file, start the service, and wait until the daemon runs and keeps running for a moment."""
        self.start(spec)
        return self.wait_running()

    def wait_running(self) -> Status:
        status = self.status()
        for _ in range(int(START_WAIT_SECONDS / POLL_SECONDS)):
            if status.running:
                break
            self._sleep(POLL_SECONDS)
            status = self.status()
        if not status.running:
            return status
        self._sleep(SETTLE_SECONDS)
        return self.status()


class Launchd(Manager):
    name = "launchd"
    tool = "launchctl"
    service = LABEL

    @staticmethod
    def name_of(agent: str | None) -> str:
        return label_of(agent)

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{self.service}"

    def file(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{self.service}.plist"

    def render(self, spec: Spec) -> str:
        return plistlib.dumps(build_plist(spec)).decode("utf-8")

    def _facts(self) -> dict[str, str] | None:
        """``launchctl print`` of the job, parsed; None when launchd has no such job loaded."""
        proc = self._call("print", self.target, check=False)
        if proc.returncode != 0:
            return None
        return parse_launchctl_print(proc.stdout)

    def _bootout(self) -> None:
        self._call("bootout", self.target, timeout=STOP_SECONDS + CALL_TIMEOUT)
        for _ in range(int((STOP_SECONDS + RESTART_SECONDS) / POLL_SECONDS)):
            if self._facts() is None:
                return
            self._sleep(POLL_SECONDS)
        raise ServiceError(f"launchd still has {self.target} {STOP_SECONDS + RESTART_SECONDS} seconds after bootout")

    def start(self, spec: Spec) -> None:
        if self._facts() is not None:
            self._bootout()
        spec.output.parent.mkdir(parents=True, exist_ok=True)  # launchd will not create it
        path = self._write(spec)
        self._call("bootstrap", self.domain, str(path))

    def stop(self) -> bool:
        loaded = self._facts() is not None
        if loaded:
            self._bootout()
        path = self.file()
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed or loaded

    def status(self) -> Status:
        facts = self._facts()
        status = Status(
            manager=self.name,
            name=self.service,
            file=str(self.file()),
            installed=self.file().exists(),
            loaded=facts is not None,
            running=False,
        )
        if facts is None:
            return status
        status.state = facts.get("state")
        status.running = status.state == "running"
        pid = facts.get("pid", "")
        status.pid = int(pid) if pid.isdigit() else None
        last_exit = facts.get("last exit code")
        status.last_exit = None if last_exit in (None, "(never exited)") else last_exit
        runs = facts.get("runs", "")
        status.restarts = max(int(runs) - 1, 0) if runs.isdigit() else None
        return status

    def _plist(self) -> dict | None:
        try:
            with open(self.file(), "rb") as handle:
                data = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def program(self) -> str | None:
        data = self._plist() or {}
        args = data.get("ProgramArguments")
        return shlex.join(str(arg) for arg in args) if isinstance(args, list) else None

    def installed_environment(self) -> dict[str, str]:
        env = (self._plist() or {}).get("EnvironmentVariables")
        if not isinstance(env, dict):
            return {}
        return {key: value for key, value in env.items() if isinstance(key, str) and isinstance(value, str)}


class Systemd(Manager):
    name = "systemd"
    tool = "systemctl"
    service = UNIT

    @staticmethod
    def name_of(agent: str | None) -> str:
        return unit_of(agent)

    def _base_args(self) -> list[str]:
        return [self.tool, "--user"]

    def file(self) -> Path:
        config = os.environ.get("XDG_CONFIG_HOME") or ""
        base = Path(config) if os.path.isabs(config) else Path.home() / ".config"
        return base / "systemd" / "user" / self.service

    def render(self, spec: Spec) -> str:
        return build_unit(spec)

    def slice_file(self) -> Path:
        return self.file().parent / SLICE

    def _write(self, spec: Spec) -> Path:
        path = super()._write(spec)
        write_atomic(self.slice_file(), build_slice().encode("utf-8"), FILE_MODE)
        return path

    def _facts(self) -> dict[str, str]:
        proc = self._call("show", self.service, f"--property={','.join(SYSTEMD_PROPERTIES)}")
        facts = {}
        for line in proc.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                facts[key.strip()] = value.strip()
        return facts

    def start(self, spec: Spec) -> None:
        self._write(spec)
        self._call("daemon-reload")
        self._call("enable", self.service)
        self._call("restart", self.service, timeout=STOP_SECONDS + CALL_TIMEOUT)

    def stop(self) -> bool:
        path = self.file()
        existed = path.exists()
        loaded = self._facts().get("LoadState") == "loaded"
        if loaded:
            self._call("disable", "--now", self.service, timeout=STOP_SECONDS + CALL_TIMEOUT)
        path.unlink(missing_ok=True)
        if existed or loaded:
            self._call("daemon-reload")
            self._call("reset-failed", self.service, check=False)
        return existed or loaded

    def status(self) -> Status:
        facts = self._facts()
        active, sub = facts.get("ActiveState"), facts.get("SubState")
        pid = facts.get("MainPID", "")
        restarts = facts.get("NRestarts", "")
        return Status(
            manager=self.name,
            name=self.service,
            file=str(self.file()),
            installed=self.file().exists(),
            loaded=facts.get("LoadState") == "loaded",
            running=active == "active" and sub == "running",
            state=f"{active}/{sub}" if active and sub else active,
            pid=int(pid) if pid.isdigit() and pid != "0" else None,
            last_exit=facts.get("ExecMainStatus") or None,
            restarts=int(restarts) if restarts.isdigit() else None,
        )

    def _lines(self, key: str) -> list[str]:
        try:
            text = self.file().read_text(encoding="utf-8")
        except OSError:
            return []
        prefix = f"{key}="
        return [line[len(prefix) :] for line in text.splitlines() if line.startswith(prefix)]

    def program(self) -> str | None:
        lines = self._lines("ExecStart")
        return lines[0].replace("%%", "%").replace("$$", "$") if lines else None

    def installed_environment(self) -> dict[str, str]:
        """The variables of the unit's Environment= lines, unquoted as systemd reads them, a later one winning."""
        found: dict[str, str] = {}
        for line in self._lines("Environment"):
            try:
                words = shlex.split(line)
            except ValueError:
                continue
            for word in words:
                key, sep, value = word.partition("=")
                if key and sep:
                    found[key] = value.replace("%%", "%")
        return found

    @staticmethod
    def lingering() -> bool:
        """Whether systemd keeps this user's services running after they log out (``loginctl enable-linger``)."""
        try:
            user = getpass.getuser()
        except (KeyError, OSError):
            return False
        return Path("/var/lib/systemd/linger", user).exists()


def manager(platform: str | None = None, run=None, sleep=None, agent: str | None = None) -> Manager:
    """The service manager of this machine, for the node of ``agent`` (default: ``$EVO_WORKER_AGENT``, else the one
    node of the machine)."""
    platform = sys.platform if platform is None else platform
    agent = agent_handle(agent if agent is not None else os.environ.get(AGENT_VARIABLE))
    if platform == "darwin":
        return Launchd(run, sleep, agent)
    if platform.startswith("linux"):
        return Systemd(run, sleep, agent)
    raise ServiceError(
        f"evo-agents worker service needs launchd (macOS) or systemd (Linux), not {platform}; run "
        "`evo-agents worker run` under a supervisor of your own instead"
    )


# The commands


def _command(func):
    """Turn the failures of ``func`` into an ``error:`` line and exit status 1."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except (ServiceError, WorkerStateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


def missing_extra() -> str | None:
    """The module of the worker extra that ``worker run`` needs and this Python lacks, or None."""
    return None if importlib.util.find_spec("aiohttp") is not None else "aiohttp"


def doctor_warnings(spec: Spec) -> list[str]:
    """The warning lines of ``evo-agents worker doctor`` on this machine, with the PATH of ``spec``."""
    from evo_agents.worker import doctor

    return doctor.warnings(doctor.examine(doctor.Machine.here(path=spec.path)))


def self_update_warning() -> str | None:
    """A warning when the install this command runs from, which the service will run, cannot update itself to
    nightly (a pipx or pip install, an editable one): the daemon would stay on this version until a person installs
    it again."""
    from evo_agents.worker.selfupdate.install import here

    reason = here().reason
    return f"warning: the daemon will not update itself to nightly: {reason}" if reason else None


def _refuse_foreign_daemon(home: WorkerHome, service: Manager) -> None:
    pid = home.read_pid()
    if pid is None:
        return
    status = service.status()
    if status.running and status.pid == pid:
        return
    raise ServiceError(
        f"a worker daemon runs here already outside the service (pid {pid}): stop it first (Ctrl-C where it runs, "
        f"or kill -TERM {pid}), then install again"
    )


def _describe(status: Status) -> str:
    if status.running:
        return f"running, pid {status.pid}" if status.pid else "running"
    if not status.loaded:
        return "not loaded"
    details = [status.state] if status.state and status.state != "not running" else []
    if status.last_exit is not None:
        details.append(f"last exit {status.last_exit}")
    return f"not running ({', '.join(details)})" if details else "not running"


def _logs(service: Manager, home: Path) -> str:
    log = home / "worker.log"
    if isinstance(service, Systemd):
        return f"{log}; before it opens: journalctl --user -u {service.service}"
    return f"{log}; before it opens: {home / 'service.log'}"


def _manager_of(args) -> Manager:
    """The manager of the node ``--agent`` names (default: ``$EVO_WORKER_AGENT``, else the one node of the machine)."""
    return manager(agent=getattr(args, "agent", None))


@_command
def cmd_install(args) -> int:
    from evo_agents.worker.cli_support import EXTRA_HINT

    home = WorkerHome()
    service = _manager_of(args)
    if service.agent and not os.environ.get(HOME_VARIABLE):
        raise ServiceError(
            f"the node of agent {service.agent} needs a state directory of its own: set {HOME_VARIABLE} (for "
            f"example ~/.evo/worker-{service.agent}) for `worker join` and for this command, so it does not share "
            "the directory of another node"
        )
    home.load_config()  # a machine that is not a worker yet: the daemon would only fail and be started again
    home.load_token()
    missing = missing_extra()
    if missing:
        print(
            f"error: evo-agents worker service install needs the worker extra (missing module {missing}): {EXTRA_HINT}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    _refuse_foreign_daemon(home, service)
    spec = build_spec(home, kept=service.installed_environment(), agent=service.agent)
    home.ensure()
    status = service.install(spec)
    found = runtimes_on(spec.path)
    print(f"Installed {service.service} ({service.name}): {service.file()}")
    print(
        f"  runs: {shlex.join(spec.program)}, now and at each login, and again {RESTART_SECONDS} s after it exits "
        "with an error"
    )
    on_path = ", ".join(f"{name} {path}" for name, path in found.items() if path) or "none"
    print(f"  on the PATH it keeps: {on_path}")
    absent = [name for name, path in found.items() if not path]
    if absent:
        print(f"  not on that PATH: {', '.join(absent)}; install again once they are, so the daemon finds them")
    if spec.kept:
        print(f"  kept from the service installed before: {', '.join(spec.kept)}")
    print(f"  state: {spec.home}")
    print(f"  log: {_logs(service, spec.home)}")
    if isinstance(service, Systemd) and not Systemd.lingering():
        print(
            "  systemd stops user services when you log out; to keep the worker running without a session: "
            "loginctl enable-linger"
        )
    try:
        warned = doctor_warnings(spec)
    except Exception as exc:  # the doctor only warns: nothing it meets stops the install
        warned = [f"warning: `evo-agents worker doctor` did not finish ({type(exc).__name__}: {exc})"]
    try:
        stays = self_update_warning()
    except Exception as exc:  # it only warns, like the doctor
        stays = f"warning: could not read whether the daemon can update itself ({type(exc).__name__}: {exc})"
    for line in [*warned, *([stays] if stays else [])]:
        print(line, file=sys.stderr)
    if not status.running:
        print(
            f"error: the service is installed, but the daemon is {_describe(status)}: see {_logs(service, spec.home)}",
            file=sys.stderr,
        )
        return EXIT_FAILED
    print(f"  daemon: {_describe(status)}")
    return 0


@_command
def cmd_uninstall(args) -> int:
    service = _manager_of(args)
    if service.stop():
        print(f"Uninstalled {service.service} ({service.name}): the daemon is stopped and {service.file()} deleted.")
    else:
        print(f"{service.service} ({service.name}) is not installed.")
    return 0


@_command
def cmd_status(args) -> int:
    service = _manager_of(args)
    status = service.status()
    home = service.installed_home() or Path(os.path.abspath(WorkerHome().root))
    if args.json:
        report = {**status.to_json(), "program": service.program(), "home": str(home), "log": str(home / "worker.log")}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not status.installed and not status.loaded:
        print(f"{service.service} ({service.name}) is not installed: `evo-agents worker service install` starts it.")
        return 0
    print(f"{service.service} ({service.name}): {_describe(status)}")
    print(f"  file: {status.file}{'' if status.installed else ' (missing; uninstall, then install again)'}")
    program = service.program()
    if program:
        print(f"  runs: {program}")
    if status.restarts:
        print(f"  started again {status.restarts} time(s) since it was loaded")
    if status.running and status.last_exit not in (None, "0"):
        print(f"  last exit: {status.last_exit}")
    print(f"  state: {home}")
    print(f"  log: {_logs(service, home)}")
    return 0


def register(wsub) -> None:
    """``evo-agents worker service install|uninstall|status`` under the parser group of ``evo-agents worker``."""
    service = wsub.add_parser(
        "service",
        help="keep the daemon running in the background: a LaunchAgent on macOS, a systemd user unit on Linux",
    )
    ssub = service.add_subparsers(dest="service_command", required=True)
    agent_help = (
        "the node of this agent (handle): a service of its own, next to the nodes of other agents; needs "
        f"{HOME_VARIABLE} for its state directory. Default: ${AGENT_VARIABLE}, else the one node of the machine"
    )
    install = ssub.add_parser(
        "install",
        help="run the daemon now and at each login, again after it exits with an error, with PATH as it is now; "
        "installing again keeps the EVO_WORKER_* variables the installed service sets",
    )
    install.add_argument("--agent", metavar="HANDLE", help=agent_help)
    install.set_defaults(func=cmd_install)
    uninstall = ssub.add_parser("uninstall", help="stop the daemon and remove the service")
    uninstall.add_argument("--agent", metavar="HANDLE", help=agent_help)
    uninstall.set_defaults(func=cmd_uninstall)
    status = ssub.add_parser("status", help="whether the service is installed and the daemon runs")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument("--agent", metavar="HANDLE", help=agent_help)
    status.set_defaults(func=cmd_status)
