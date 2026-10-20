"""The other side of a run's socket (``evo_agents.worker.credentials``, which answers it), each a command a process
of the run calls:

- ``git_credential``: ``evo-agents worker git-credential --run N get|store|erase``, the run's helper in git's
  credential protocol; store and erase do nothing.
- ``print_env``: ``evo-agents worker env --run N``, what the run's leases add to its agent's environment.
- ``ask_push``: a Builder's ``evo-agents worker step`` asks the daemon to push the run's branch of a repo.
- ``ask_open``: a chat run's ``evo-agents worker repo open`` asks the daemon to open a repo of the project.

Each asks the daemon over ``credentials.ask``. Standard library only, as ``credentials``, so git's helper starts
without the worker extra.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from urllib.parse import urlsplit

from evo_agents.worker import credentials
from evo_agents.worker.home import WorkerHome

MAX_INPUT_BYTES = 64 * 1024  # what git writes to its helper
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")  # a variable print_env exports


def read_attributes(stream) -> dict[str, str]:
    """git's ``key=value`` lines up to an empty line or the end, ``url=`` taken apart into protocol, host and path."""
    found: dict[str, str] = {}
    size = 0
    for line in stream:
        size += len(line)
        line = line.rstrip("\r\n")
        if not line or size > MAX_INPUT_BYTES:
            break
        key, sep, value = line.partition("=")
        if sep:
            found[key] = value
    url = found.get("url")
    if url and "host" not in found:
        parts = urlsplit(url)
        found.setdefault("protocol", parts.scheme)
        if parts.hostname:
            found["host"] = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
        found.setdefault("path", parts.path.lstrip("/"))
    return found


def _line_safe(value) -> bool:
    return isinstance(value, str) and bool(value) and not any(char in value for char in "\n\r\0")


def git_credential(run_id: int, action: str, stdin, stdout, stderr, home: WorkerHome | None = None) -> int:
    """``evo-agents worker git-credential --run N ACTION``: for ``get``, the username and password of run N's lease
    for what git asks, from the run's socket; ``store``, ``erase`` and any other action read git's lines and do
    nothing. Nothing is printed when the run holds no lease for it: git then fails to authenticate."""
    attributes = read_attributes(stdin)
    if action != "get":
        return 0
    request = {"op": "git", **{key: attributes.get(key, "") for key in ("protocol", "host", "path")}}
    ticket = os.environ.get(credentials.TICKET_VARIABLE)
    if ticket:
        request["ticket"] = ticket
    answer = credentials.ask(home or WorkerHome(), run_id, request)
    if answer is None:
        stderr.write(f"evo-agents: run {run_id} holds no credentials on this worker\n")
        return 0
    username, password = answer.get("username"), answer.get("password")
    if _line_safe(username) and _line_safe(password):
        stdout.write(f"username={username}\npassword={password}\n")
    return 0


def ask_push(home: WorkerHome, run_id: int, repo: str, title: str | None) -> dict:
    """``evo-agents worker step`` of a Builder of the Curator: ask the daemon to push the run's branch of ``repo``;
    its answer (``branch``, ``head``, ``changed``, ``default``, ``commits``, or ``error``)."""
    timeout = credentials.PUSH_TIMEOUT + credentials.SOCKET_TIMEOUT
    answer = credentials.ask(home, run_id, {"op": "push", "repo": repo, "title": title}, timeout=timeout)
    return answer if answer is not None else {"error": f"run {run_id} holds no socket on this worker to push through"}


def ask_open(home: WorkerHome, run_id: int, repo: str) -> dict:
    """``evo-agents worker repo open`` of a chat run's agent: ask the daemon to open ``repo`` of the project in the
    run; its answer (``repo``, ``path``, ``opened``, or ``error``)."""
    timeout = credentials.OPEN_TIMEOUT + credentials.SOCKET_TIMEOUT
    answer = credentials.ask(home, run_id, {"op": "open", "repo": repo}, timeout=timeout)
    return answer if answer is not None else {"error": f"run {run_id} holds no socket on this worker to open a repo"}


def print_env(
    run_id: int, stdout, stderr, home: WorkerHome | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """``evo-agents worker env --run N``: ``export NAME=value`` lines of what run N's leases add to its agent's
    environment, for a shell to evaluate; 1 when the run holds no socket here, or answers nothing (a run of the
    Curator asked without its pane's ticket, ENV_TICKET_VARIABLE)."""
    request: dict = {"op": "env"}
    ticket = (os.environ if environ is None else environ).get(credentials.ENV_TICKET_VARIABLE)
    if ticket:
        request["ticket"] = ticket
    answer = credentials.ask(home or WorkerHome(), run_id, request)
    env = answer.get("env") if isinstance(answer, dict) else None
    if not isinstance(env, dict):
        stderr.write(f"evo-agents: run {run_id} holds no credentials on this worker\n")
        return 1
    for key, value in sorted(env.items()):
        if NAME.match(str(key)) and isinstance(value, str) and "\0" not in value:
            stdout.write(f"export {key}={shlex.quote(value)}\n")
    return 0
