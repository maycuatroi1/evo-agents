"""``evo-agents worker browser login NAME``: a person signs a browser identity in on the worker's own machine.

No run is involved. The hub makes the identity when it does not exist yet (``browser_local``, pinned to this worker)
and hands out its profile; it is unpacked into a directory of its own (0700), Chromium opens on it with a window on
this screen (``chromium.start(headed=True)``), and the person signs in and closes the window. A second Chromium, with
no window, then deletes every cookie outside the identity's domains (``profiles._keep_only``), the profile is packed
without caches and goes back as the version after the one it started from. A window closed with nothing saved, or a
profile the hub refuses (the identity moved, a run holds it), saves nothing and the directory is removed.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import shutil
import sys
import tempfile
from pathlib import Path

from evo_agents.hub import browser_archive
from evo_agents.worker.browser import chromium, install, profiles
from evo_agents.worker.home import WorkerHome, WorkerStateError

CLEANUP_LOG = "cleanup.log"
LOGIN_LOG = "login.log"


def check_screen() -> None:
    """WorkerStateError on a Linux machine with no display for Chromium's window."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise WorkerStateError("this machine has no display (DISPLAY is not set): sign in from a desktop session")


async def _wait_closed(process) -> None:
    while process.poll() is None:
        await asyncio.sleep(profiles.POLL)


async def _keep_own_cookies(installed, directory: Path, domains: tuple[str, ...], env) -> bool:
    """Open the profile without a window, delete what is not the identity's; whether nothing foreign is left."""
    async with profiles.own_chromium(installed, directory, directory.parent / CLEANUP_LOG, env) as tools:
        cookies = (await tools("Storage.getCookies")).get("cookies") or []
        return await profiles._keep_only(tools, domains, cookies)


async def login(home: WorkerHome, ask: dict, *, say=print) -> dict:
    """Run one local login of the identity ``ask`` names (the body of ``POST .../browser-identities/login``); the
    hub's answer to the save, ``{identity_id, version}``."""
    from evo_agents.worker import hubapi

    installed = install.read(home)
    if installed is None:
        raise WorkerStateError("no Chromium on this worker: run `evo-agents worker browser install` first")
    check_screen()
    config, token = home.load_config(), home.load_token()
    env = dict(os.environ)
    async with hubapi.new_session() as session:
        hub = hubapi.WorkerHub(config.url, token, session)
        try:
            lease = await hub.local_browser_login(ask)
        except hubapi.HubProblem as exc:
            raise exc.hub_error() from None
        identity_id, base = int(lease["identity_id"]), int(lease["version"])
        domains = tuple(lease.get("domains") or ())
        work = Path(tempfile.mkdtemp(prefix="evo-browser-login-"))
        os.chmod(work, chromium.DIR_MODE)
        directory = work / "profile"
        try:
            if lease.get("profile"):
                try:
                    archive = base64.b64decode(lease["profile"], validate=True)
                    if profiles.newer(lease.get("chromium_version"), installed.version):
                        raise WorkerStateError(
                            f"Chromium {lease['chromium_version']} saved this profile, a later version than "
                            f"{installed.version}: run `evo-agents worker browser install` again"
                        )
                    await asyncio.to_thread(browser_archive.unpack, archive, directory)
                except (binascii.Error, browser_archive.ProfileArchiveError, OSError) as exc:
                    raise WorkerStateError(f"the saved profile does not unpack ({exc}): sign in from scratch") from None
            first = ask.get("login_url") or lease.get("login_url") or f"https://{domains[0]}/"
            process = await asyncio.to_thread(
                chromium.start,
                installed.executable,
                directory,
                installed.version,
                sandbox=installed.sandbox,
                libraries=installed.libraries,
                base_env=env,
                log_path=work / LOGIN_LOG,
                headed=True,
            )
            say(
                f"Chromium is open on identity {lease['name']} ({', '.join(domains)}). Sign in at {first}, then close the window."
            )
            try:
                await _wait_closed(process)
            finally:
                await asyncio.to_thread(chromium.kill, process, 1.0)
            try:
                kept = await _keep_own_cookies(installed, directory, domains, env)
            except (profiles.DevToolsFailed, OSError) as exc:
                raise WorkerStateError(
                    f"Chromium did not reopen the profile to clean it ({exc}): nothing saved"
                ) from None
            if not kept:
                raise WorkerStateError("cookies outside the identity's domains could not be deleted: nothing saved")
            archive = await asyncio.to_thread(browser_archive.pack, directory)
            try:
                return await hub.save_local_browser_profile(
                    identity_id, archive, base_version=base, chromium_version=installed.version
                )
            except hubapi.HubProblem as exc:
                raise exc.hub_error() from None
        finally:
            await asyncio.to_thread(shutil.rmtree, work, True)
