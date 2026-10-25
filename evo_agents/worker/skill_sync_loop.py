"""The skill sync of a worker pool node's daemon (feature ``runs.SKILL_SYNC``; plan worker-pools, criterion a5).

A machine that joined a pool with a join token (``evo-agents worker join --token``) has no ``hub login``, yet its
agents want the skills its owner's machines have. When it starts and every ``runs.SKILL_SYNC_EVERY_SECONDS`` (6
hours) the daemon of such a node runs ``hub skills sync`` (``evo_agents.hub.skill_sync.SkillSync``) in a thread with
its worker token: the global skills its owner reads go into the skills directory of each runtime installed here, each
with its ``.evo-hub.json`` marker, as the owner's sync would write them on a machine without a harness. The worker
token reads only GET /v1/worker/skills and the bundles of those skills (``evo_agents.hub.server.skills``), nothing of a
run; the node has no project of its own here, so project skills stay with the harnesses.

A node is a pool node when its labels name a pool (``pool=<name>``, which joining with a token writes); any other
daemon skips the loop. A hub of an older release answers 404, and a worker that is not a pool node 403: either is
logged at info and tried again at the next turn. Any other failure is a warning, and the loop goes on.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

from evo_agents.hub import runs
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.skill_sync import Report, SkillSync

log = logging.getLogger(__name__)

POOL_LABEL = "pool="
SKILLS = "/v1/skills"
WORKER_SKILLS = "/v1/worker/skills"


def pool_of(labels: Sequence[str]) -> str | None:
    """The pool a daemon's labels name, None for a worker outside a pool."""
    return next((label[len(POOL_LABEL) :] for label in labels if label.startswith(POOL_LABEL)), None)


class NodeHub(Hub):
    """The hub as ``SkillSync`` asks it, answered through the worker routes a pool node's token reads: no projects,
    and the global skills and their bundles under /v1/worker/skills."""

    def __init__(self, url: str, token: str, timeout: float | None = None):
        options = {} if timeout is None else {"timeout": timeout}
        super().__init__(url, token, headers={runs.PROTOCOL_HEADER: runs.PROTOCOL_VERSION}, **options)

    def call(self, method: str, path: str, body: dict | None = None, *, timeout: float | None = None):
        if method == "GET" and path == "/v1/projects":
            return []  # a node syncs no project skills: their harnesses are not on it
        if path == SKILLS or path.startswith((SKILLS + "?", SKILLS + "/global/")):
            path = WORKER_SKILLS + path[len(SKILLS) :]
        else:
            raise HubError(f"a pool node does not call {method} {path}")
        return super().call(method, path, body, timeout=timeout)


def sync_once(url: str, token: str, env: Mapping[str, str]) -> Report:
    """One ``hub skills sync`` of this machine's runtimes, with the worker token ``token``."""
    home = Path(env["HOME"]) if env.get("HOME") else None
    return SkillSync(NodeHub(url, token), home=home, env=env).run()


async def sync_now(url: str, token: str, env: Mapping[str, str]) -> Report | None:
    """Sync in a thread; the report, or None when the sync failed (logged)."""
    try:
        report = await asyncio.to_thread(sync_once, url, token, env)
    except HubError as exc:
        level = logging.INFO if exc.status in (403, 404) else logging.WARNING
        log.log(level, "skills not synced", extra={"status": exc.status, "error": str(exc)})
        return None
    except Exception:  # a bug of the sync must not end the daemon's loop
        log.exception("the skill sync failed")
        return None
    extra = {"counts": report.counts(), "targets": len(report.targets)}
    if report.errors:
        log.warning("skills synced with errors", extra={**extra, "errors": report.errors})
    else:
        log.info("skills synced", extra=extra)
    return report


async def sync_loop(
    url: str,
    token: str,
    env: Mapping[str, str],
    stopping: asyncio.Event,
    *,
    every: float = runs.SKILL_SYNC_EVERY_SECONDS,
) -> None:
    """Sync now, then every ``every`` seconds, until ``stopping`` is set."""
    while not stopping.is_set():
        await sync_now(url, token, env)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stopping.wait(), every)
