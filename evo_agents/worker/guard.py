"""The worker's part of the core plugin guard (``docs/guard.md``): it keeps the hub's policy of private names and puts
the guards of ``evo_agents.guard_check`` in front of every run.

- The heartbeat answer's ``settings.guard`` is the digest of the hub's policy, empty when it guards nothing. When it
  differs from the policy the worker holds, the daemon fetches GET /v1/worker/guard/policy and writes it to
  ``<home>/guard/policy.json`` (``take``), or removes the file for an empty digest. The daemon's environment names the
  file in EVO_GUARD_POLICY from its start, so its own pushes (``gitops._send``) and every run's agent read it.
- ``check_push`` refuses a push of the daemon's own (``gitops._send``) that sends a name to a guarded repo.
- ``agent_env`` gives a run's agent, while the policy guards something, the ``gh`` shim in front of its PATH
  (``<home>/guard/bin/gh``), and ``authorship.hook_script`` has the run's git hooks check commit messages and pushes.
  A run in a container of an environment gets neither: its git and PATH are the image's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from evo_agents.guard_check import ENV, SHIM_ENV
from evo_agents.private_names import Policy

log = logging.getLogger(__name__)

DIRECTORY = "guard"
POLICY_FILE = "policy.json"


def policy_path(root: Path) -> Path:
    return Path(root) / DIRECTORY / POLICY_FILE


def bin_dir(root: Path) -> Path:
    return Path(root) / DIRECTORY / "bin"


def held_digest(root: Path) -> str:
    """The digest of the policy the worker holds, empty for none."""
    try:
        data = json.loads(policy_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("digest") or "") if isinstance(data, dict) else ""


def write(root: Path, data: dict) -> None:
    """Keep the policy the hub answered, after checking it reads; ValueError for one that does not."""
    Policy.from_json(data)
    path = policy_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def clear(root: Path) -> None:
    policy_path(root).unlink(missing_ok=True)


def shim_text() -> str:
    """The ``gh`` shim: this machine's Python runs ``evo_agents.guard_check gh`` with the arguments (``-I``: a
    checkout's own files never stand in for evo_agents)."""
    return f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I -m evo_agents.guard_check gh "$@"\n'


def shim(root: Path) -> Path:
    """The directory of the ``gh`` shim, written when missing or stale."""
    directory = bin_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gh"
    text = shim_text()
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    path.chmod(0o755)
    return directory


def on(env: dict[str, str]) -> bool:
    """Whether the policy ``env`` names guards something now."""
    from evo_agents.guard_check import load

    return load(env.get(ENV)) is not None


def agent_env(env: dict[str, str], root: Path) -> dict[str, str]:
    """What a run's agent gets for the guard, after ``env``: the shim first on PATH while the policy guards something;
    nothing otherwise."""
    if not on(env):
        return {}
    directory = shim(root)
    return {SHIM_ENV: str(directory), "PATH": os.pathsep.join([str(directory), env.get("PATH", "")]).rstrip(os.pathsep)}


async def take(hub, root: Path, digest: str) -> bool:
    """Bring the policy the worker holds to the hub's ``digest``: fetch and keep it, or remove it for an empty digest;
    whether it changed. A fetch that fails leaves the one held, for the next heartbeat to try again."""
    if digest == held_digest(root):
        return False
    if not digest:
        clear(root)
        log.info("guard policy removed: the hub guards nothing")
        return True
    data = await hub.call("GET", "/v1/worker/guard/policy")
    if not isinstance(data, dict):
        raise ValueError("the hub answered no guard policy")
    write(root, data)
    found = Policy.from_json(data)
    log.info("guard policy taken", extra={"terms": len(found.terms), "targets": len(found.targets)})
    return True


def heard(running: asyncio.Task | None, hub, root: Path, settings) -> asyncio.Task | None:
    """The fetch of the policy for the digest a heartbeat's ``settings`` carry (``settings.guard``; a hub before 0192
    sends none, which leaves the policy held): a new task for a digest new to the worker, else ``running``."""
    digest = settings.get("guard") if isinstance(settings, dict) else None
    if not isinstance(digest, str) or hub is None or (running is not None and not running.done()):
        return running
    if digest == held_digest(root):
        return running
    return asyncio.create_task(_fetch(hub, root, digest))


async def _fetch(hub, root: Path, digest: str) -> None:
    try:
        await take(hub, root, digest)
    except Exception:  # the next heartbeat tries again; the policy held stays
        log.warning("the hub's guard policy could not be taken", exc_info=True)


def check_push(cwd: Path, source: str, branch: str, remote: str, env: Mapping[str, str] | None) -> None:
    """``gitops.PushRefused`` when the remote is a repo the policy EVO_GUARD_POLICY of ``env`` guards and a commit
    ``source`` would send holds a private name, in its message, a path or a line it adds: every push of the daemon
    goes through it (``gitops._send``)."""
    from evo_agents import guard_check
    from evo_agents.worker.gitops import PushRefused

    policy = guard_check.load((os.environ if env is None else env).get(guard_check.ENV))
    if policy is None:
        return
    found = subprocess.run(
        ["git", "remote", "get-url", "--push", remote], cwd=cwd, capture_output=True, text=True, check=False
    )
    url = found.stdout.strip() if found.returncode == 0 and found.stdout.strip() else remote
    if not policy.guards(url):
        return
    text = guard_check.outgoing(cwd, source, remote=remote)
    try:
        guard_check.check_text(policy, f"{branch}\n{text}", f"the push of {branch} to {url}")
    except guard_check.Refused as exc:
        raise PushRefused(str(exc)) from None
