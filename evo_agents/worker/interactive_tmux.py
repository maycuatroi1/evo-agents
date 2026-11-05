"""tmux for interactive runs (``evo_agents.worker.interactive``): the server the worker uses, its sessions, and the
script a terminal UI starts through, which sets the agent's environment whatever the tmux server's is."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from evo_agents.worker.runtimes import common

SESSION_NAME = "evo-run-{id}"
TMUX_SOCKET_VARIABLE = "EVO_WORKER_TMUX_SOCKET"  # a tmux server of the worker's own (tests use one)
TMUX_TIMEOUT = 15.0  # seconds for one tmux command
WINDOW = (200, 50)  # columns and rows of a new tmux session, until a client attaches
# Variables the UI's script leaves as tmux sets them in the pane.
KEPT_BY_TMUX = frozenset({"TERM", "TMUX", "TMUX_PANE", "COLUMNS", "LINES", "SHLVL", "PWD", "OLDPWD", "_"})
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def session_name(run_id: int) -> str:
    """The tmux session of run ``run_id``: ``evo-run-N``, as the web's Take over dialog prints it."""
    return SESSION_NAME.format(id=int(run_id))


def exact_target(name: str) -> str:
    """A target-session that matches ``name`` only (``evo-run-1`` alone would also find ``evo-run-12``)."""
    return f"={name}"


def pane_target(name: str) -> str:
    """The active pane of session ``name``, matched exactly."""
    return f"={name}:"


def _cut(text: str, limit: int = 300) -> str:
    return common.cut(text, limit)


def _socket_of(tmux_variable: str | None) -> str | None:
    """The server socket in ``$TMUX`` (``/private/tmp/tmux-501/default,1234,0``)."""
    if not tmux_variable:
        return None
    return tmux_variable.split(",", 1)[0] or None


def default_socket(env: Mapping[str, str]) -> str:
    """The socket tmux uses without -S or -L: ``$TMUX_TMPDIR/tmux-UID/default``, /tmp by default."""
    return os.path.join(env.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", "default")


class TmuxError(RuntimeError):
    pass


class Tmux:
    """tmux on one server: ``socket``, or the default one. Its commands run without ``$TMUX``, so they reach that
    server from inside another tmux as well."""

    def __init__(self, binary: str | None, socket: str | None, env: Mapping[str, str]):
        self.binary = binary
        self.socket = socket
        self.env = {key: value for key, value in env.items() if key not in ("TMUX", "TMUX_PANE")}

    @classmethod
    def from_env(cls, env: Mapping[str, str], socket: str | None = None) -> Tmux:
        binary = shutil.which("tmux", path=env.get("PATH"))
        return cls(binary, socket or env.get(TMUX_SOCKET_VARIABLE) or _socket_of(env.get("TMUX")), env)

    @property
    def available(self) -> bool:
        return self.binary is not None

    def argv(self, *args: str) -> list[str]:
        if self.binary is None:
            raise TmuxError("tmux is not on PATH")
        return [self.binary, *(("-S", self.socket) if self.socket else ()), *args]

    async def run(self, *args: str, timeout: float = TMUX_TIMEOUT) -> tuple[int, str]:
        """(exit status, what it printed) of one tmux command; 124 when it gave no answer in time."""
        proc = await asyncio.create_subprocess_exec(
            *self.argv(*args),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            return 124, f"tmux gave no answer within {timeout:g}s"
        assert proc.returncode is not None  # communicate() waited for it
        return proc.returncode, out.decode(errors="replace").strip()

    def run_sync(self, *args: str, timeout: float = TMUX_TIMEOUT) -> tuple[int, str]:
        try:
            done = subprocess.run(
                self.argv(*args),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                env=self.env,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 124, f"tmux gave no answer within {timeout:g}s"
        except OSError as exc:
            return 127, str(exc)
        return done.returncode, (done.stdout + done.stderr).strip()

    def version(self) -> str | None:
        if not self.available:
            return None
        code, out = self.run_sync("-V", timeout=5)
        return out.split()[-1] if code == 0 and out else None

    async def start(
        self,
        name: str,
        argv: list[str],
        env: Mapping[str, str],
        *,
        cwd: Path,
        scratch: Path,
        drop: Iterable[str] = (),
        size: tuple[int, int] = WINDOW,
        withheld: Iterable[str] = (),
        env_command: str | None = None,
    ) -> None:
        """A detached session ``name`` whose one pane runs ``argv`` in ``cwd`` with ``env``, but for ``withheld``, which
        ``env_command`` prints (``write_script``). A session of that name left by an earlier daemon is killed first."""
        await self.kill(name)
        script = write_script(scratch / f"{name}.sh", argv, env, cwd, drop, withheld=withheld, env_command=env_command)
        try:
            code, out = await self.run(
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                str(size[0]),
                "-y",
                str(size[1]),
                "-c",
                str(cwd),
                shlex.quote(str(script)),
            )
        except BaseException:
            script.unlink(missing_ok=True)
            raise
        if code != 0:
            script.unlink(missing_ok=True)
            raise TmuxError(f"tmux new-session failed: {_cut(out)}")

    async def alive(self, name: str) -> bool:
        """Whether session ``name`` is there with a pane still running (a pane kept by remain-on-exit is not). A tmux
        that does not answer in time counts as alive: only a missing session ends the person's turn."""
        code, out = await self.run("list-panes", "-s", "-t", pane_target(name), "-F", "#{pane_dead}")
        if code == 124:
            return True
        return code == 0 and any(line.strip() in ("", "0") for line in out.splitlines())

    def has_session(self, name: str) -> bool:
        code, _ = self.run_sync("has-session", "-t", exact_target(name))
        return code == 0

    async def kill(self, name: str) -> None:
        if self.available:
            await self.run("kill-session", "-t", exact_target(name))

    def kill_sync(self, name: str) -> None:
        if self.available:
            self.run_sync("kill-session", "-t", exact_target(name))

    async def pipe(self, name: str, path: Path) -> None:
        """Append what the pane of ``name`` prints to ``path`` (tmux pipe-pane)."""
        path.touch(mode=0o600, exist_ok=True)
        command = f"cat >> {shlex.quote(str(path))}"
        code, out = await self.run("pipe-pane", "-o", "-t", pane_target(name), command)
        if code != 0:
            raise TmuxError(f"tmux pipe-pane failed: {_cut(out)}")

    async def socket_path(self, name: str) -> str | None:
        """The path of the server's socket, as tmux names it, for ``evo-agents worker attach``."""
        code, out = await self.run("display-message", "-p", "-t", pane_target(name), "#{socket_path}")
        return out if code == 0 and out.startswith("/") else None

    def attach_command(self, name: str, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
        """The command that puts the terminal of ``env`` on session ``name``: ``switch-client`` from inside a client of
        the same server, ``attach-session`` otherwise."""
        inside = _socket_of(env.get("TMUX"))
        ours = self.socket or default_socket(env)
        if inside is not None and os.path.realpath(inside) == os.path.realpath(ours):
            return self.argv("switch-client", "-t", exact_target(name)), dict(env)
        clean = {key: value for key, value in env.items() if key not in ("TMUX", "TMUX_PANE")}
        return self.argv("attach-session", "-t", exact_target(name)), clean


def _locale(env: Mapping[str, str]) -> dict[str, str]:
    """A UTF-8 locale for the UI when the agent's environment names none (a service starts with almost nothing)."""
    if any(env.get(name) for name in ("LC_ALL", "LC_CTYPE", "LANG")):
        return {}
    return {"LANG": "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"}


def write_script(
    path: Path,
    argv: list[str],
    env: Mapping[str, str],
    cwd: Path,
    drop: Iterable[str] = (),
    *,
    withheld: Iterable[str] = (),
    env_command: str | None = None,
) -> Path:
    """The script tmux runs in the pane: it removes itself, enters ``cwd``, exports ``env`` over what the tmux server
    gives a pane (but for what tmux sets per pane), unsets ``drop`` and executes ``argv``. Mode 0700, as the run's
    directory it lies in.

    The variables in ``withheld``, which the run's leases set, are left out of the script: it evaluates the export
    lines ``env_command`` prints instead (``evo-agents worker env --run N``), after the other variables and before the
    unsets, so the values never touch the disk."""
    dropped, hidden = set(drop), set(withheld)
    lines = ["#!/bin/sh", 'rm -f -- "$0"', f"cd -- {shlex.quote(str(cwd))} || exit 1"]
    for key, value in {**env, **_locale(env)}.items():
        if key in KEPT_BY_TMUX or key in dropped or key in hidden or not _NAME.match(key) or "\0" in value:
            continue
        lines.append(f"export {key}={shlex.quote(value)}")
    if env_command:
        lines.append(f'eval "$({env_command})"')
    for key in sorted(dropped):
        if _NAME.match(key):
            lines.append(f"unset {key}")
    lines.append("exec " + " ".join(shlex.quote(part) for part in argv))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o700)
    return path
