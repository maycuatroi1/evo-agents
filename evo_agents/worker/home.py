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
  worktrees/<project>-<run>/    the run's git worktree, removed 7 days after the run ended
```

Every file is replaced atomically (``evo_agents.hub.client.write_atomic``), so a crash leaves the old file or the new
one. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
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
    allow_web_terminal: bool = False
    owner: str | None = None
    token_id: int | None = None
    joined_at: str | None = None
    # project -> {"workspace": str | None, "repos": [{"name", "path", "default_branch"}]}, as the hub lists them
    repos: dict[str, dict] = field(default_factory=dict)
    # "<project>/<repo>" -> a path, set by hand; wins over what the registry and the hub say
    checkouts: dict[str, str] = field(default_factory=dict)

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
        return config


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
        directory = self.run_dir(record["id"])
        directory.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        data = json.dumps(record, indent=2, ensure_ascii=False, default=str).encode() + b"\n"
        write_atomic(directory / "run.json", data, FILE_MODE)

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

    def remove_run(self, run_id: int) -> None:
        shutil.rmtree(self.run_dir(run_id), ignore_errors=True)

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
