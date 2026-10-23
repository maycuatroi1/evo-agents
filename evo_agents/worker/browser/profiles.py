"""The browser identities of a run on its worker (plan agent-browser, criterion a4): their profiles come from the hub
before the run's Chromium starts, and go back to it, saved, as the run ends.

A run dispatched with identities names them in its spec (``browser_identities``, the ids in the order of the
dispatch). Before Chromium starts, ``Profiles.prepare`` asks the hub for each (``WorkerHub.browser_profile``: the
hub answers only the worker holding the run, for an identity the run holds), and unpacks the profile of the first into
``runs/<run>/browser/profile`` (``browser_archive.unpack``, which removes the Singleton* files). The profile of each
other identity goes to ``runs/<run>/browser/identity-<id>``, and a Chromium of its own on it, for as long as it takes
to read them, gives its cookies of the identity's domains, which ``restore`` sets in the run's Chromium once it
listens. An identity never saved starts empty.

A profile this worker's Chromium cannot open says so to the hub, which sets the identity needs_login
(``WorkerHub.browser_needs_login``), and the run goes on without it, with a note in its log: a profile saved by a
Chromium of a later major version than this worker's (cause newer_chromium, nothing of the identity is saved after
the run, so the profile stays for that Chromium), or one that does not unpack, or that Chromium does not start on
(cause unopenable; the run starts on an empty profile, and saves what it signs into over the one that did not open).

``Profiles.save``, as the run ends and before its final report (``Agent.save_browser``), while the run still holds
its identities: the cookies of the run's Chromium are read (Storage.getCookies), every cookie outside the first
identity's domains is deleted from it (Network.deleteCookies, on a page of its own) and Chromium is closed, then its
profile is packed without caches (``browser_archive.pack``) and sent with the version it started from
(``WorkerHub.save_browser_profile``): a hub where the identity moved on meanwhile refuses it and keeps its version.
For each other identity, a Chromium on its own profile gets the cookies of its domains in place of those it had, then
that profile goes the same way. A save that does not go through is logged and noted, and the run ends as it would
have; no cookie, profile or archive is ever logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from evo_agents.hub import browser_archive
from evo_agents.worker.browser import chromium
from evo_agents.worker.hubapi import HubProblem
from evo_agents.worker.runner.common import RunFailed, log

if TYPE_CHECKING:
    from evo_agents.worker.browser.install import Installed
    from evo_agents.worker.browser.session import BrowserSession

OTHER = "identity-{id}"  # the profile of an identity after the first, in the run's browser directory
OTHER_LOG = "identity-{id}.log"
CDP_TIMEOUT = 20.0  # seconds a DevTools command has to answer
START_TIMEOUT = 30.0  # seconds a Chromium of an identity's own has to write its DevTools port
CLOSE_TIMEOUT = 10.0  # seconds a Chromium has to end after Browser.close
POLL = 0.1
# The keys of a cookie Storage.getCookies says that Storage.setCookies takes back (Network.CookieParam)
COOKIE_KEYS = ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite", "priority", "partitionKey")


class DevToolsFailed(Exception):
    """A Chromium that did not start, or a DevTools command that failed or did not answer."""


@dataclass
class Identity:
    """One browser identity of the run, as the hub handed it."""

    id: int
    name: str
    domains: tuple[str, ...]
    version: int  # the version of its profile the run started from
    chromium_version: str | None  # of the Chromium that saved it
    login_url: str | None = None  # the page its login session opens
    archive: bytes | None = None  # its profile, until it is unpacked
    saving: bool = True  # whether its profile goes back to the hub as the run ends
    cookies: list[dict] = field(default_factory=list)  # of its domains, for an identity after the first


def owns(domains: Iterable[str], cookie_domain: str) -> bool:
    """Whether a cookie of ``cookie_domain`` (``.example.com`` or ``example.com``) is of one of ``domains``: the
    domain itself or a subdomain of it."""
    host = cookie_domain.lstrip(".").lower()
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _major(version: str | None) -> int | None:
    head = (version or "").split(".", 1)[0]
    return int(head) if head.isdigit() else None


def newer(saved_by: str | None, installed: str) -> bool:
    """Whether a profile saved by Chromium ``saved_by`` comes from a later major version than ``installed``."""
    saved, here = _major(saved_by), _major(installed)
    return saved is not None and here is not None and saved > here


def cookie_param(cookie: dict) -> dict:
    """``cookie`` as Storage.getCookies said it, as Storage.setCookies takes it: a session cookie stays one."""
    param = {key: cookie[key] for key in COOKIE_KEYS if cookie.get(key) is not None}
    expires = cookie.get("expires")
    if not cookie.get("session") and isinstance(expires, int | float) and expires > 0:
        param["expires"] = expires
    return param


class DevTools:
    """One DevTools websocket of a Chromium: a command to the browser, or to a page with ``session``."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse):
        self.socket = socket
        self.last = 0

    async def __call__(self, method: str, params: dict | None = None, *, session: str | None = None) -> dict:
        self.last += 1
        message = {"id": self.last, "method": method, "params": params or {}}
        if session is not None:
            message["sessionId"] = session
        try:
            await self.socket.send_json(message)
            while True:
                answer = await self.socket.receive_json(timeout=CDP_TIMEOUT)
                if answer.get("id") == self.last:
                    break
        except (TimeoutError, aiohttp.ClientError, TypeError, ValueError, OSError) as exc:
            raise DevToolsFailed(f"{method}: {exc or type(exc).__name__}") from None
        if "error" in answer:
            raise DevToolsFailed(f"{method}: {answer['error'].get('message', 'refused')}")
        return answer.get("result") or {}

    async def close_browser(self) -> None:
        """Browser.close: Chromium ends writing its profile and exits, and the socket closes with it."""
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError, OSError):
            await self.socket.send_json({"id": self.last + 1, "method": "Browser.close", "params": {}})


@contextlib.asynccontextmanager
async def devtools(cdp: str, socket: str) -> AsyncIterator[DevTools]:
    """The DevTools of the browser at ``cdp`` (http://127.0.0.1:<port>), over its websocket ``socket``."""
    url = cdp.replace("http://", "ws://", 1) + socket
    try:
        async with aiohttp.ClientSession() as http, http.ws_connect(url, max_msg_size=0) as connected:
            yield DevTools(connected)
    except (aiohttp.ClientError, OSError) as exc:
        raise DevToolsFailed(f"no DevTools at {cdp}: {exc}") from None


async def exited(process: subprocess.Popen, timeout: float) -> bool:
    """Wait at most ``timeout`` seconds for ``process`` to exit; whether it did."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if process.poll() is not None:
            return True
        await asyncio.sleep(POLL)
    return process.poll() is not None


@contextlib.asynccontextmanager
async def own_chromium(installed: Installed, profile: Path, log_path: Path, env) -> AsyncIterator[DevTools]:
    """A Chromium of its own on ``profile``, for as long as the block takes: closed with Browser.close at its end so
    it ends writing the profile, and killed with what is left of its group in any case."""
    process = await asyncio.to_thread(
        chromium.start,
        installed.executable,
        profile,
        installed.version,
        sandbox=installed.sandbox,
        libraries=installed.libraries,
        base_env=env,
        log_path=log_path,
    )
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT
        while (found := chromium.port(profile)) is None and process.poll() is None and loop.time() < deadline:
            await asyncio.sleep(POLL)
        if found is None:
            raise DevToolsFailed(chromium.tail(log_path) or "Chromium wrote no DevTools port")
        async with devtools(f"http://127.0.0.1:{found[0]}", found[1]) as tools:
            yield tools
            await tools.close_browser()
        await exited(process, CLOSE_TIMEOUT)
    finally:
        await asyncio.to_thread(chromium.kill, process, 1.0)


class Profiles:
    """The browser identities of the run of ``session`` (``ids``, in the order of its dispatch)."""

    def __init__(self, session: BrowserSession, ids: Iterable[int]):
        self.session = session
        self.ids = [int(identity_id) for identity_id in ids]
        self.identities: list[Identity] = []
        self.notes: list[str] = []  # for the run's log
        self.installed: Installed | None = None
        self.unpacked = False  # whether the first identity's profile is where the run's Chromium starts
        self.saved = False
        self.sent: list[int] = []  # the identities whose profile the hub took

    @property
    def login_url(self) -> str | None:
        """The page a login session opens: the first identity's login URL."""
        return self.identities[0].login_url if self.identities else None

    @property
    def hub(self):
        return self.session.daemon.hub

    def take_notes(self) -> list[str]:
        notes, self.notes = self.notes, []
        return notes

    async def prepare(self, installed: Installed) -> None:
        """Ask the hub for each identity and lay out its profile; RunFailed with cause environment when it hands
        one not."""
        self.installed = installed
        for identity_id in self.ids:
            self.identities.append(await self._lease(identity_id))
        if not self.identities:
            return
        first, *others = self.identities
        self.unpacked = await self._unpack(first, self.session.profile)
        for identity in others:
            await self._read_cookies(identity)

    async def _lease(self, identity_id: int) -> Identity:
        run_id = self.session.run_id
        try:
            answer = await self.hub.browser_profile(run_id, identity_id)
            profile = answer.get("profile")
            identity = Identity(
                id=identity_id,
                name=str(answer.get("name") or identity_id),
                domains=tuple(answer.get("domains") or ()),
                version=int(answer["version"]),
                chromium_version=answer.get("chromium_version"),
                login_url=answer.get("login_url") or None,
                archive=base64.b64decode(profile, validate=True) if profile else None,
            )
        except (HubProblem, KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise RunFailed(
                f"the hub did not hand the run its browser identity {identity_id}: {exc}", cause="environment"
            ) from None
        log.info(
            "browser identity leased",
            extra={"run_id": run_id, "browser_identity": identity_id, "version": identity.version},
        )
        return identity

    async def _unpack(self, identity: Identity, directory: Path) -> bool:
        """Unpack ``identity``'s profile into ``directory``; whether there was one and it unpacked."""
        archive, identity.archive = identity.archive, None
        if archive is None:
            return False
        if newer(identity.chromium_version, self.installed.version):
            identity.saving = False
            why = f"Chromium {identity.chromium_version} saved it, a later version than {self.installed.version}"
            await self.cannot_open(identity, "newer_chromium", why)
            return False
        try:
            await asyncio.to_thread(browser_archive.unpack, archive, directory)
        except (browser_archive.ProfileArchiveError, OSError) as exc:
            await asyncio.to_thread(shutil.rmtree, directory, True)
            await self.cannot_open(identity, "unopenable", str(exc))
            return False
        return True

    async def cannot_open(self, identity: Identity, cause: str, why: str) -> None:
        """The run goes on without ``identity``'s profile: the hub sets it needs_login, and the run's log says so."""
        self.notes.append(
            f"Browser: the profile of browser identity {identity.name} does not open on this worker ({why}); the "
            "run goes on without what it was signed into, and the identity waits for a new login."
        )
        try:
            await self.hub.browser_needs_login(self.session.run_id, identity.id, cause)
        except HubProblem as exc:
            log.warning(
                "browser identity not set needs_login",
                extra={"run_id": self.session.run_id, "browser_identity": identity.id, "error": str(exc)},
            )

    def _other(self, identity: Identity) -> tuple[Path, Path]:
        directory = self.session.directory
        return directory / OTHER.format(id=identity.id), directory / OTHER_LOG.format(id=identity.id)

    async def _read_cookies(self, identity: Identity) -> None:
        profile, log_path = self._other(identity)
        if not await self._unpack(identity, profile):
            return
        try:
            async with own_chromium(self.installed, profile, log_path, self.session.daemon.env) as tools:
                cookies = (await tools("Storage.getCookies")).get("cookies") or []
        except (DevToolsFailed, OSError) as exc:
            await asyncio.to_thread(shutil.rmtree, profile, True)
            await self.cannot_open(identity, "unopenable", f"Chromium did not start on it: {exc}")
            return
        identity.cookies = [cookie for cookie in cookies if owns(identity.domains, cookie.get("domain", ""))]

    async def unopenable(self) -> bool:
        """The run's Chromium did not start on the first identity's profile and started without it: say so; whether
        there was such a profile."""
        if not self.unpacked:
            return False
        self.unpacked = False
        await self.cannot_open(self.identities[0], "unopenable", "Chromium did not start on it")
        return True

    async def restore(self) -> None:
        """Set the cookies of the identities after the first in the run's Chromium, which listens now."""
        cookies = [cookie_param(cookie) for identity in self.identities[1:] for cookie in identity.cookies]
        if not cookies:
            return
        try:
            async with devtools(self.session.cdp, self.session.socket) as tools:
                await tools("Storage.setCookies", {"cookies": cookies})
        except DevToolsFailed as exc:
            log.warning("browser identity cookies not set", extra={"run_id": self.session.run_id, "error": str(exc)})
            self.notes.append("Browser: the cookies of the run's other browser identities were not set in Chromium.")

    async def save(self) -> None:
        """Send each identity's profile back to the hub, once, from the run's Chromium, which this closes."""
        session = self.session
        process = session.process
        if self.saved or not self.identities or process is None or process.poll() is not None or not session.cdp:
            return
        self.saved = True
        first, *others = self.identities
        try:
            async with devtools(session.cdp, session.socket) as tools:
                cookies = (await tools("Storage.getCookies")).get("cookies") or []
                for identity in others:
                    identity.cookies = [
                        cookie for cookie in cookies if owns(identity.domains, cookie.get("domain", ""))
                    ]
                if first.saving:
                    first.saving = await _keep_only(tools, first.domains, cookies)
                await tools.close_browser()
        except (DevToolsFailed, KeyError) as exc:
            self._not_saved(self.identities, f"Chromium did not answer: {exc}")
            return
        if not await exited(process, CLOSE_TIMEOUT):
            self._not_saved(self.identities, f"Chromium did not exit within {CLOSE_TIMEOUT:g}s of Browser.close")
            return
        if first.saving:
            await self._send(first, session.profile)
        for identity in others:
            if identity.saving:
                await self._save_other(identity)

    async def _save_other(self, identity: Identity) -> None:
        profile, log_path = self._other(identity)
        try:
            async with own_chromium(self.installed, profile, log_path, self.session.daemon.env) as tools:
                await tools("Storage.clearCookies")
                if identity.cookies:
                    await tools("Storage.setCookies", {"cookies": [cookie_param(c) for c in identity.cookies]})
        except (DevToolsFailed, OSError) as exc:
            self._not_saved([identity], f"Chromium did not start on its profile: {exc}")
            return
        await self._send(identity, profile)

    async def _send(self, identity: Identity, profile: Path) -> None:
        run_id = self.session.run_id
        try:
            archive = await asyncio.to_thread(browser_archive.pack, profile)
            answer = await self.hub.save_browser_profile(
                run_id,
                identity.id,
                archive,
                base_version=identity.version,
                chromium_version=self.installed.version if self.installed else None,
            )
        except (HubProblem, OSError) as exc:
            self._not_saved([identity], str(exc))
            return
        self.sent.append(identity.id)
        log.info(
            "browser profile saved",
            extra={
                "run_id": run_id,
                "browser_identity": identity.id,
                "version": answer.get("version"),
                "bytes": len(archive),
            },
        )

    def _not_saved(self, identities: Iterable[Identity], why: str) -> None:
        for identity in identities:
            if not identity.saving:
                continue
            log.warning(
                "browser profile not saved",
                extra={"run_id": self.session.run_id, "browser_identity": identity.id, "error": why},
            )
            self.notes.append(
                f"Browser: the profile of browser identity {identity.name} was not saved ({why}); it stays at "
                f"version {identity.version}."
            )


async def _keep_only(tools: DevTools, domains: tuple[str, ...], cookies: list[dict]) -> bool:
    """Delete from the browser every cookie of ``cookies`` outside ``domains``; whether none is left."""
    foreign = [cookie for cookie in cookies if not owns(domains, cookie.get("domain", ""))]
    if not foreign:
        return True
    target = (await tools("Target.createTarget", {"url": "about:blank"}))["targetId"]
    page = (await tools("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
    for cookie in foreign:
        params = {key: cookie[key] for key in ("name", "domain", "path", "partitionKey") if cookie.get(key)}
        await tools("Network.deleteCookies", params, session=page)
    left = (await tools("Storage.getCookies")).get("cookies") or []
    kept = all(owns(domains, cookie.get("domain", "")) for cookie in left)
    if not kept:
        log.warning("browser cookies outside the identity's domains were left", extra={"left": len(left)})
    return kept
