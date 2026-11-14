"""The worker's state on this machine, under ``~/.evo/worker`` (or ``$EVO_WORKER_HOME``).

```
~/.evo/worker/            0700
  config.json             0600  the hub, the worker as the hub registered it, the repos of its projects
  token                   0600  the worker token (evw_...), shown by the hub once
  worker.log              0600  JSON lines, tokens removed
  service.log                   what the daemon prints before worker.log opens, under launchd (``service``)
  daemon.pid                    the pid of the running daemon, locked while it runs
  spool/<run>.jsonl, .ack       a run's events until the hub acknowledges them
  runs/<run>/run.json           what the daemon knows of a run: its worktree, branch, base, when it ended
  runs/<run>/events.jsonl       every event of the run, uploaded as its log when it ends
  runs/<run>/decisions.jsonl    the decisions a plan run's agent asked (``evo-agents worker ask``), one id a line
  runs/<run>/browser-logins.jsonl  the logins a run's agent asked (``evo-agents worker browser login``): decision, identity
  runs/<run>/pushes.jsonl       the commits ``evo-agents worker step`` of a plan run pushed to a default branch, one
                                push a line, which a later replay onto that branch never puts back (``gitops.Replay``)
  runs/<run>/push_conflict.json the side branch ``evo-agents worker step`` pushed a plan run's commits to instead of its
                                default branch (``gitops.PushConflict``): the daemon fails the run once the turn ends
  runs/<run>/agent.json         the process group of the run's latest agent while the run has not ended here, for the
                                next daemon to stop when this one dies first (``orphans``)
  runs/<run>/cred.sock    0600  the unix socket the daemon hands the run's leases through, to its own uid only, while
                                the run holds them (``credentials``)
  worktrees/<project>-<run>/    the run's git worktree, removed 7 days after the run ended (30 for a parked run),
                                at once when its work merged or the drive has less free than disk_reserve_gb; for a
                                plan run, the agent's directory: a worktree of each repo and .evo-run/ (plan.yaml,
                                result.json)
```

Every file is replaced atomically (``evo_agents.hub.client.write_atomic``), so a crash leaves the old file or the new
one. Standard library only.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub.client import write_atomic

DIR_MODE = 0o700
FILE_MODE = 0o600
HOME_VARIABLE = "EVO_WORKER_HOME"
JOIN_HINT = "join this machine first: `evo-agents worker join --url URL --code CODE` or `evo-agents worker register`"
# `worker run` on a machine that is not a worker (any more), or a daemon the hub no longer takes, exits with this
# status: starting it again changes nothing. EVO_WORKER_REVOKED_EXIT names another, for a service manager that tells
# exits apart only as 0 or not (launchd).
EXIT_REVOKED = 3
REVOKED_EXIT_VARIABLE = "EVO_WORKER_REVOKED_EXIT"
# A daemon that updated its install to the tag nightly, or found it updated by another daemon of the same install, exits
# with this status (EX_TEMPFAIL of sysexits.h): neither 0 nor EXIT_REVOKED, so launchd and systemd start the new one.
EXIT_UPDATED = 75
DISK_RESERVE_GB = 20.0  # WorkerConfig.disk_reserve_gb by default, the hub's own reserve (ops_events.DISK_LOW_GB)
# the headroom over the reserve the cleanup makes room up to when WorkerConfig.disk_headroom_gb says none: the larger
# of these gigabytes and this share of the reserve (``disk_headroom``), as the hub's band (ops_events.disk_headroom)
DISK_HEADROOM_GB = 5.0
DISK_HEADROOM_SHARE = 0.25


class WorkerStateError(Exception):
    """The state on disk is missing or unreadable; ``str`` says what to do."""


class NotJoined(WorkerStateError):
    pass


@dataclass
class WorkerConfig:
    url: str
    worker_id: int
    name: str
    projects: list[str]
    slots: int = 1
    labels: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)  # whose runtimes it takes, as its owner set them on the hub
    allow_web_terminal: bool = False
    # its owner's lock on the version, as the hub last sent it (0048); None until a hub sent one. A daemon the hub stops
    # with 426 updates itself only when this says False (``selfupdate``)
    version_locked: bool | None = None
    owner: str | None = None
    token_id: int | None = None
    joined_at: str | None = None
    # project -> {"workspace": str | None, "repos": [{"name", "path", "default_branch"}], "harness": str | None},
    # as the hub lists them
    repos: dict[str, dict] = field(default_factory=dict)
    # "<project>/<repo>" -> a path, set by hand; wins over what the registry and the hub say
    checkouts: dict[str, str] = field(default_factory=dict)
    # the gigabytes the drive of worktrees/ keeps free: below it the daemon removes the worktrees of ended runs no
    # parked or running run needs, and claims no run while it stays below (``daemon_cleanup.Cleanup.make_room``)
    disk_reserve_gb: float = DISK_RESERVE_GB
    # the gigabytes over the reserve the cleanup frees once the drive fell below it, so free disk does not hover at the
    # reserve; None: ``disk_headroom`` of the reserve
    disk_headroom_gb: float | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data) -> WorkerConfig:
        if not isinstance(data, dict):
            raise ValueError("config.json is not a JSON object")
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        config = cls(**known)
        if not isinstance(config.url, str) or not isinstance(config.worker_id, int) or not config.name:
            raise ValueError("config.json lacks the hub URL, the worker id or the worker name")
        if not isinstance(config.projects, list) or not all(isinstance(p, str) for p in config.projects):
            raise ValueError("config.json has no list of projects")
        reserve = config.disk_reserve_gb
        if isinstance(reserve, bool) or not isinstance(reserve, int | float) or reserve < 0:
            config.disk_reserve_gb = DISK_RESERVE_GB  # a value set by hand that is not a size: the default holds
        headroom = config.disk_headroom_gb
        if isinstance(headroom, bool) or not isinstance(headroom, int | float | None) or (headroom or 0) < 0:
            config.disk_headroom_gb = None
        return config


def disk_headroom(reserve_gb: float) -> float:
    """The gigabytes over a reserve of ``reserve_gb`` the cleanup makes room up to by default: the larger of
    ``DISK_HEADROOM_GB`` and ``DISK_HEADROOM_SHARE`` of the reserve."""
    return max(DISK_HEADROOM_GB, DISK_HEADROOM_SHARE * reserve_gb)


def dir_bytes(path: Path) -> int:
    """The bytes of the files directly in directory ``path``; 0 when it is not one."""
    return sum(item.stat().st_size for item in path.iterdir() if item.is_file()) if path.is_dir() else 0


def revoked_exit(env: Mapping[str, str]) -> int:
    """The exit status for a machine that is no longer a worker: EVO_WORKER_REVOKED_EXIT when it holds a status from
    0 to 255, else EXIT_REVOKED."""
    try:
        value = int(env.get(REVOKED_EXIT_VARIABLE) or EXIT_REVOKED)
    except ValueError:
        return EXIT_REVOKED
    return value if 0 <= value <= 255 else EXIT_REVOKED


def default_root() -> Path:
    """``$EVO_WORKER_HOME``, else ``~/.evo/worker``; the background service pins the variable to the directory it was
    installed with."""
    return Path(os.environ.get(HOME_VARIABLE) or "~/.evo/worker").expanduser()


class WorkerHome:
    """The paths of the state directory, and reading and writing what lies there."""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()
        self.config_path = self.root / "config.json"
        self.token_path = self.root / "token"
        self.log_path = self.root / "worker.log"
        self.pid_path = self.root / "daemon.pid"
        self.spool_dir = self.root / "spool"
        self.runs_dir = self.root / "runs"
        self.worktrees_dir = self.root / "worktrees"
        self.recovery_path = self.root / "recovery.jsonl"  # the WIP commits of merged runs' worktrees removed

    def ensure(self) -> None:
        """Create the directories, mode 0700 whatever the umask, and narrow an older directory that is wider."""
        for directory in (self.root, self.spool_dir, self.runs_dir, self.worktrees_dir):
            directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
            os.chmod(directory, DIR_MODE)

    # Credentials and configuration

    def joined(self) -> bool:
        return self.config_path.exists() and self.token_path.exists()

    def save(self, config: WorkerConfig, token: str | None = None) -> None:
        self.ensure()
        if token is not None:
            write_atomic(self.token_path, token.encode() + b"\n", FILE_MODE)
        data = json.dumps(config.to_json(), indent=2, ensure_ascii=False).encode() + b"\n"
        write_atomic(self.config_path, data, FILE_MODE)

    def load_config(self) -> WorkerConfig:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotJoined(f"this machine is not a worker yet: {JOIN_HINT}") from None
        except (OSError, ValueError) as exc:
            raise WorkerStateError(f"cannot read {self.config_path} ({exc}): {JOIN_HINT}") from None
        try:
            return WorkerConfig.from_json(data)
        except (TypeError, ValueError) as exc:
            raise WorkerStateError(f"{self.config_path} is not a worker configuration ({exc}): {JOIN_HINT}") from None

    def load_token(self) -> str:
        try:
            token = self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            raise NotJoined(f"this machine has no worker token: {JOIN_HINT}") from None
        except OSError as exc:
            raise WorkerStateError(f"cannot read {self.token_path} ({exc})") from None
        if not token:
            raise WorkerStateError(f"{self.token_path} is empty: {JOIN_HINT}")
        return token

    def forget(self) -> None:
        """Delete the token and the configuration; spool, runs and worktrees stay for the cleanup."""
        for path in (self.token_path, self.config_path):
            path.unlink(missing_ok=True)

    # Runs

    def run_dir(self, run_id: int) -> Path:
        return self.runs_dir / str(int(run_id))

    def worktree_path(self, project: str, run_id: int) -> Path:
        return self.worktrees_dir / f"{project}-{int(run_id)}"

    def save_run(self, record: dict) -> None:
        """Write the record of a run. When the atomic write fails (a full disk has no room for the temporary file),
        the record is written over the one there in place, as compact JSON, which fits the blocks the old one holds:
        the end of a run (``finished_at``) is kept even then."""
        directory = self.run_dir(record["id"])
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        path = directory / "run.json"
        data = json.dumps(record, indent=2, ensure_ascii=False, default=str).encode() + b"\n"
        try:
            write_atomic(path, data, FILE_MODE)
        except OSError:
            if not path.is_file():
                raise
            compact = json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str).encode() + b"\n"
            _write_in_place(path, compact)

    def load_runs(self) -> list[dict]:
        records = []
        if not self.runs_dir.is_dir():
            return records
        for directory in sorted(self.runs_dir.iterdir()):
            with contextlib.suppress(OSError, ValueError):
                record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
                if isinstance(record, dict) and isinstance(record.get("id"), int):
                    records.append(record)
        return records

    def load_run(self, run_id: int) -> dict | None:
        """The record of run ``run_id`` on this machine, or None."""
        with contextlib.suppress(OSError, ValueError):
            record = json.loads((self.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
            if isinstance(record, dict) and record.get("id") == int(run_id):
                return record
        return None

    def decisions_path(self, run_id: int) -> Path:
        """Where ``evo-agents worker ask`` notes each decision a plan run's agent asked, for the daemon to wait on."""
        return self.run_dir(run_id) / "decisions.jsonl"

    def browser_logins_path(self, run_id: int) -> Path:
        """Where ``evo-agents worker browser login`` notes each login its agent asked, for the daemon to save the
        profile when the answer is Done (``browser.logins``)."""
        return self.run_dir(run_id) / "browser-logins.jsonl"

    def pushes_path(self, run_id: int) -> Path:
        """Where ``evo-agents worker step`` notes each commit a plan run pushed to a default branch."""
        return self.run_dir(run_id) / "pushes.jsonl"

    def note_push(self, run_id: int, repo: str, branch: str, head: str) -> None:
        """Note that plan run ``run_id`` pushed ``head`` to ``branch`` of ``repo``'s origin."""
        directory = self.run_dir(run_id)
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        line = json.dumps({"repo": repo, "branch": branch, "head": head}) + "\n"
        with open(self.pushes_path(run_id), "a", encoding="utf-8") as handle:
            handle.write(line)

    def pushed(self, run_id: int, repo: str, branch: str) -> list[str]:
        """The commits plan run ``run_id`` pushed to ``branch`` of ``repo``'s origin, as ``note_push`` noted them."""
        found: list[str] = []
        with contextlib.suppress(OSError):
            for line in self.pushes_path(run_id).read_text(encoding="utf-8").splitlines():
                with contextlib.suppress(ValueError, AttributeError):
                    item = json.loads(line)
                    head = item.get("head")
                    if item.get("repo") == repo and item.get("branch") == branch and isinstance(head, str) and head:
                        found.append(head)
        return found

    def note_recovery(self, entry: dict) -> None:
        """Add a line to recovery.jsonl for the uncommitted changes of a run's worktree kept before it went: when, the
        run, the repo, the branch, HEAD, the WIP commit, the ref that names it and the checkout it lies in."""
        line = json.dumps({"at": datetime.now(UTC).isoformat(), **entry}) + "\n"
        fd = os.open(self.recovery_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)

    def conflict_path(self, run_id: int) -> Path:
        """Where ``evo-agents worker step`` notes a push of a plan run that went to its side branch instead."""
        return self.run_dir(run_id) / "push_conflict.json"

    def save_conflict(self, run_id: int, conflict: dict) -> None:
        directory = self.run_dir(run_id)
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        data = json.dumps({"run_id": int(run_id), **conflict}, indent=2, ensure_ascii=False).encode() + b"\n"
        write_atomic(self.conflict_path(run_id), data, FILE_MODE)

    def load_conflict(self, run_id: int) -> dict | None:
        """What ``save_conflict`` wrote for run ``run_id`` (an ``error`` among it), or None."""
        with contextlib.suppress(OSError, ValueError):
            found = json.loads(self.conflict_path(run_id).read_text(encoding="utf-8"))
            if isinstance(found, dict) and found.get("run_id") == int(run_id) and isinstance(found.get("error"), str):
                return found
        return None

    def remove_run(self, run_id: int) -> None:
        shutil.rmtree(self.run_dir(run_id), ignore_errors=True)

    # The agent of a run that has not ended

    def agent_path(self, run_id: int) -> Path:
        return self.run_dir(run_id) / "agent.json"

    def save_agent(self, run_id: int, agent: dict) -> None:
        """Note the process of the agent that run ``run_id`` started last (``orphans.describe``)."""
        directory = self.run_dir(run_id)
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        data = json.dumps({"run_id": int(run_id), **agent}, indent=2, ensure_ascii=False).encode() + b"\n"
        write_atomic(self.agent_path(run_id), data, FILE_MODE)

    def load_agents(self) -> dict[int, dict]:
        """run id -> what agent.json says, for every run with one; a file that does not read is left out."""
        found: dict[int, dict] = {}
        if not self.runs_dir.is_dir():
            return found
        for path in sorted(self.runs_dir.glob("*/agent.json")):
            with contextlib.suppress(OSError, ValueError):
                agent = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(agent, dict) and agent.get("run_id") == int(path.parent.name):
                    found[agent["run_id"]] = agent
        return found

    def remove_agent(self, run_id: int) -> None:
        self.agent_path(run_id).unlink(missing_ok=True)

    # The graphs it keeps fresh

    def kg_sync(self) -> list[dict]:
        """What the daemon's last look at each project whose graph it keeps fresh found (``evo_agents.worker.kg_sync``),
        as its heartbeat reports it, by project."""
        from evo_agents.worker.kg_sync import shown_state

        return shown_state(self.root)

    def kg_sync_lines(self) -> list[str]:
        """The same, as ``evo-agents worker status`` prints it."""
        from evo_agents.worker.kg_sync import status_lines

        return status_lines(self.kg_sync())

    # The daemon's pid

    def read_pid(self) -> int | None:
        """The pid in daemon.pid when a process holds its lock; None when no daemon runs."""
        try:
            text = self.pid_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text.isdigit():
            return None
        if not _locked(self.pid_path):
            return None
        return int(text)


def _locked(path: Path) -> bool:
    """Whether another process holds the lock of ``path`` (``PidLock``)."""
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not POSIX
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class PidLock:
    """daemon.pid, locked for as long as the daemon runs, so a second daemon on the machine refuses to start."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> bool:
        import fcntl

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        with contextlib.suppress(OSError):
            os.ftruncate(self.fd, 0)
        os.close(self.fd)  # closing drops the lock
        self.fd = None


def _write_in_place(path: Path, data: bytes) -> None:
    """Write ``data`` over ``path`` from its start and cut what follows: no new file, so no new inode or directory
    entry, which a full disk may refuse. Not atomic: only for when ``write_atomic`` could not be."""
    fd = os.open(path, os.O_WRONLY)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.ENOSPC, f"wrote {len(data) - len(view)} of {len(data)} bytes of {path.name}")
            view = view[written:]
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)
