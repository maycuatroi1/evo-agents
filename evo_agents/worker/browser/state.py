"""``evo-agents worker browser state --out FILE`` (plan agent-browser, criterion a8): the cookies of the run's Chromium as a
Playwright storageState file, for a script of the agent's that needs the session.

The file holds secrets, so it goes only where no commit takes it: a relative path is relative to the run's own
directory on the worker (``runs/<run>/``), an absolute one must lie inside that directory, and a path in a worktree
(``worktrees/``, where the agent's repos are) or anywhere else is refused. The file is made 0600 and replaced
atomically; a link in its place is refused. Local storage is not in it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from evo_agents.worker.browser.profiles import DevTools, DevToolsFailed
from evo_agents.worker.home import WorkerStateError

if TYPE_CHECKING:
    from evo_agents.worker.home import WorkerHome

FILE_MODE = 0o600
TIMEOUT = 20.0
SAME_SITE = {"Strict": "Strict", "Lax": "Lax", "None": "None"}


def out_path(home: WorkerHome, run_id: int, out: str) -> Path:
    """Where ``out`` lies for run ``run_id``; WorkerStateError for a path in a worktree, outside the run's directory
    or at a link."""
    base = home.run_dir(run_id).resolve()
    wanted = Path(out).expanduser()
    path = (wanted if wanted.is_absolute() else base / wanted).parent.resolve() / wanted.name
    if home.worktrees_dir.resolve() in (path, *path.parents):
        raise WorkerStateError(f"{out} lies in a worktree: the session holds secrets that no commit may carry")
    if base not in path.parents:
        raise WorkerStateError(f"{out} lies outside the run's directory {base}: write the state in it")
    if path.is_symlink() or path.is_dir():
        raise WorkerStateError(f"{out} is a link or a directory: name a file")
    return path


def storage_state(cookies: list[dict]) -> dict:
    """``cookies`` as Storage.getCookies said them, as a Playwright storageState."""
    state = []
    for cookie in cookies:
        expires = cookie.get("expires")
        state.append(
            {
                "name": cookie.get("name", ""),
                "value": cookie.get("value", ""),
                "domain": cookie.get("domain", ""),
                "path": cookie.get("path", "/"),
                "expires": expires if isinstance(expires, int | float) and not cookie.get("session") else -1,
                "httpOnly": bool(cookie.get("httpOnly")),
                "secure": bool(cookie.get("secure")),
                "sameSite": SAME_SITE.get(cookie.get("sameSite"), "Lax"),
            }
        )
    return {"cookies": state, "origins": []}


async def read_cookies(cdp: str) -> list[dict]:
    """The cookies of the Chromium whose DevTools are at ``cdp`` (http://127.0.0.1:<port>)."""
    timeout = aiohttp.ClientTimeout(total=TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(f"{cdp}/json/version") as answer:
                socket_url = (await answer.json(content_type=None))["webSocketDebuggerUrl"]
            async with http.ws_connect(socket_url, max_msg_size=0) as connected:
                return (await DevTools(connected)("Storage.getCookies")).get("cookies") or []
    except (aiohttp.ClientError, DevToolsFailed, KeyError, TypeError, ValueError, OSError) as exc:
        raise WorkerStateError(f"the run's browser did not answer at {cdp}: {exc}") from None


def write(path: Path, state: dict) -> None:
    """Replace ``path`` with ``state`` as JSON, readable by its owner only."""
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(state, ensure_ascii=False))
    os.replace(temporary, path)
