"""Run a connector, in process or as an external command, and stream its kg/1 messages."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from evo_agents.harness import CONNECTOR_GROUP, Harness

DEFAULT_TIMEOUT = 1800


class ConnectorError(RuntimeError):
    """The connector could not be started: unknown name, missing credential, bad command."""


@dataclass
class ConnectorContext:
    project: str
    source: dict
    harness: Harness | None = None
    cursor: object = None
    env: dict[str, str] = field(default_factory=dict)

    @property
    def source_id(self) -> str:
        return self.source["id"]

    @property
    def harness_root(self) -> Path | None:
        return self.harness.root if self.harness else None


def _builtin(name: str) -> Callable[[ConnectorContext], Iterable[dict]] | None:
    if name == "harness":
        from evo_agents.kg.connectors.harness import run
    elif name == "git":
        from evo_agents.kg.connectors.git import run
    else:
        return None
    return run


def load_connector(name: str) -> Callable[[ConnectorContext], Iterable[dict]]:
    """A builtin name, an entry point in group evo_agents.kg.connectors, or ``python:module:function``
    for a connector that lives next to a project without being packaged."""
    found = _builtin(name)
    if found is not None:
        return found
    if name.startswith("python:"):
        module_name, _, attr = name[len("python:") :].partition(":")
        try:
            module = importlib.import_module(module_name)
            return getattr(module, attr or "run")
        except (ImportError, AttributeError) as exc:
            raise ConnectorError(f"cannot load connector {name!r}: {exc}") from exc
    for ep in metadata.entry_points(group=CONNECTOR_GROUP):
        if ep.name == name:
            return ep.load()
    raise ConnectorError(f"unknown connector {name!r}")


def credential_env(source: dict, getter: Callable[[str], str | None] | None = None) -> dict[str, str]:
    """Resolve the credentials a source declares into environment variables for its connector."""
    getter = getter or evo_cred_get
    env: dict[str, str] = {}
    for entry in source.get("credentials") or []:
        if isinstance(entry, str):
            key, name = entry, entry.upper().replace(".", "_").replace("-", "_")
        else:
            key, name = entry["key"], entry["env"]
        value = getter(key)
        if not value:
            raise ConnectorError(f"credential {key!r} for source {source['id']!r} is not available")
        env[name] = value
    return env


def evo_cred_get(key: str) -> str | None:
    evo = shutil.which("evo")
    if evo is None:
        return None
    try:
        result = subprocess.run([evo, "cred", "get", key], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


class ConnectorRun:
    """Iterate the messages of one connector run. After iteration, ``returncode``, ``exception`` and
    ``stderr_tail`` describe how the connector ended; a missing ``closed`` message is the checker's job."""

    def __init__(self, ctx: ConnectorContext, *, kill_after: int | None = None):
        self.ctx = ctx
        self.kill_after = kill_after
        self.returncode: int | None = None
        self.exception: str | None = None
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.killed = False

    def __iter__(self) -> Iterator[dict]:
        if self.ctx.source.get("connector") == "exec":
            yield from self._exec()
        else:
            yield from self._in_process()

    def _in_process(self) -> Iterator[dict]:
        run = load_connector(self.ctx.source["connector"])
        count = 0
        try:
            for message in run(self.ctx):
                if self.kill_after is not None and count >= self.kill_after:
                    self.killed = True
                    return
                count += 1
                yield message
        except Exception as exc:  # the run is reported as failed, not raised
            self.exception = f"{type(exc).__name__}: {exc}"

    def _exec(self) -> Iterator[dict]:
        command = self.ctx.source.get("command") or []
        if not command:
            raise ConnectorError(f"source {self.ctx.source_id!r} has no command")
        env = os.environ.copy()
        env.update(self.ctx.env)
        env["KG_PROJECT"] = self.ctx.project
        env["KG_SOURCE"] = json.dumps(self.ctx.source, ensure_ascii=False)
        if self.ctx.cursor is not None:
            env["KG_STATE"] = json.dumps(self.ctx.cursor, ensure_ascii=False)
        cwd = str(self.ctx.harness_root) if self.ctx.harness_root else None
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=cwd,
            )
        except OSError as exc:
            raise ConnectorError(f"cannot start {command[0]!r}: {exc}") from exc

        def drain() -> None:
            for line in proc.stderr:
                self.stderr_tail.append(line.rstrip("\n"))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timeout = float(self.ctx.source.get("timeout") or DEFAULT_TIMEOUT)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        count = 0
        drained = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if self.kill_after is not None and count >= self.kill_after:
                    self.killed = True
                    break
                count += 1
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "_invalid", "raw": line[:200]}
            else:
                drained = True
        finally:
            timer.cancel()
            if not drained and proc.poll() is None:
                # The caller stopped reading or asked for a kill: do not leave the connector running.
                proc.kill()
            self.returncode = proc.wait()
            reader.join(timeout=5)
