"""The profiles of a run's browser identities on the worker (plan agent-browser, criterion a4), with the Chromium
`evo-agents worker browser install` put in Playwright's cache: the tests that start it skip only on a machine where
that command never ran.

- An archive (``browser_archive``) holds a profile without its caches and what a process left, owned by no one;
  ``check`` and ``unpack`` refuse what is not one, an archive that unpacks to more than its limit included, and an
  unpacked profile has no Singleton* file.
- A run's Chromium starts on its first identity's profile and with the cookies of the others; as the run ends, each
  identity's profile goes back to the hub with the cookies of its own domains alone, from the version it started at.
- A profile that does not unpack, that Chromium does not start on, or that a later Chromium saved sets the identity
  needs_login, and the run goes on with an empty profile, noted in its log.
- End to end, with the hub, the daemon and a fake login site on northwind.localhost: a run signs in and its profile is
  saved, the next run of the identity on the same worker is signed in already, and the cookie of lumen.localhost the
  first run picked up went nowhere. No cookie is in a log."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import secrets
import shlex
import shutil
import sys
import tarfile
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import browser_archive
from evo_agents.worker.browser import install, profiles
from evo_agents.worker.browser import session as browser_session
from evo_agents.worker.home import WorkerHome
from evo_agents.worker.hubapi import Refused
from evo_agents.worker.runner.common import RunFailed
from tests.hub import pg
from tests.worker.test_browser_session import installed  # noqa: F401 (a fixture)

if pg.DSN:
    from tests.hub.test_browser_identity import KEY, PATH
    from tests.worker.test_daemon import WORKER, finished_cleanly, make_stack, wait_until  # noqa: F401 (a fixture)

LOGIN_WAIT = 20.0  # seconds a page has to load and its cookie to be set


# The archive


def profile_dir(root: Path) -> Path:
    profile = root / "profile"
    for name in ("Default/Cache/Cache_Data", "Default/Code Cache/js", "Default/Local Storage/leveldb", "Crashpad"):
        (profile / name).mkdir(parents=True)
    (profile / "Default" / "Cookies").write_bytes(b"cookies")
    (profile / "Default" / "Cache" / "Cache_Data" / "data_0").write_bytes(b"cache")
    (profile / "Default" / "Local Storage" / "leveldb" / "000003.log").write_bytes(b"storage")
    (profile / "Local State").write_text("{}", encoding="utf-8")
    for left in ("SingletonLock", "SingletonCookie", "DevToolsActivePort"):
        (profile / left).write_text("left", encoding="utf-8")
    (profile / "Default" / "link").symlink_to(root)
    return profile


def tar_of(*members: tuple[tarfile.TarInfo, bytes | None]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for info, data in members:
            archive.addfile(info, None if data is None else io.BytesIO(data))
    return buffer.getvalue()


def member(name: str, data: bytes = b"x", kind: bytes = tarfile.REGTYPE) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        return info, data
    info.linkname = "/etc/passwd"
    return info, None


def test_browser_profile_archive_leaves_out_caches_and_what_a_process_left_and_names_no_one(tmp_path):
    archive = browser_archive.pack(profile_dir(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as opened:
        names = set(opened.getnames())
        owners = {(info.uid, info.gid, info.uname, info.gname) for info in opened.getmembers()}
    assert names == {
        "Default",
        "Default/Cookies",
        "Default/Local Storage",
        "Default/Local Storage/leveldb",
        "Default/Local Storage/leveldb/000003.log",
        "Local State",
    }
    assert owners == {(0, 0, "", "")}
    assert browser_archive.check(archive) == len(names)

    target = tmp_path / "unpacked"
    browser_archive.unpack(archive, target)
    assert (target / "Default" / "Cookies").read_bytes() == b"cookies"
    assert not any(path.name.startswith("Singleton") for path in target.iterdir())


def test_browser_profile_archive_refuses_what_pack_never_makes(tmp_path, monkeypatch):
    refused = {
        "garbage": b"not a profile",
        "truncated": browser_archive.pack(profile_dir(tmp_path))[:40],
        "absolute": tar_of(member("/etc/cron.d/job")),
        "outside": tar_of(member("Default/../../escape")),
        "symlink": tar_of(member("Default/link", kind=tarfile.SYMTYPE)),
        "hard link": tar_of(member("Default/link", kind=tarfile.LNKTYPE)),
    }
    for why, archive in refused.items():
        with pytest.raises(browser_archive.ProfileArchiveError):
            browser_archive.check(archive)
        with pytest.raises(browser_archive.ProfileArchiveError):
            browser_archive.unpack(archive, tmp_path / why)
        assert not (tmp_path / "escape").exists(), why

    large = tar_of(member("Default/Cookies", b"x" * 4096))
    assert browser_archive.check(large) == 1
    with pytest.raises(browser_archive.ProfileArchiveError, match="more than"):
        browser_archive.check(large, limit=1024)
    monkeypatch.setattr(browser_archive, "MAX_MEMBERS", 2)
    with pytest.raises(browser_archive.ProfileArchiveError, match="more than 2 files"):
        browser_archive.check(tar_of(member("a"), member("b"), member("c")))
    # A header no member size counts, inflated past the limit and what two members' headers take: refused as it reads.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        info, data = member("Default/Cookies")
        info.pax_headers = {"comment": "x" * (64 * 1024)}
        archive.addfile(info, io.BytesIO(data))
    assert len(buffer.getvalue()) < 4096
    with pytest.raises(browser_archive.ProfileArchiveError, match="unpacks to more than"):
        browser_archive.check(buffer.getvalue(), limit=1024)


def test_browser_profile_cookie_domains_and_chromium_versions():
    domains = ("northwind.localhost",)
    assert profiles.owns(domains, "northwind.localhost") and profiles.owns(domains, ".northwind.localhost")
    assert profiles.owns(domains, "app.northwind.localhost")
    assert not profiles.owns(domains, "lumen.localhost") and not profiles.owns(domains, "notnorthwind.localhost")
    assert not profiles.owns(domains, "localhost")
    assert profiles.newer("154.0.1.2", "153.0.8010.12") and not profiles.newer("153.9.0.0", "153.0.8010.12")
    assert not profiles.newer(None, "153.0.8010.12") and not profiles.newer("odd", "153.0.8010.12")
    session = {"name": "s", "value": "v", "domain": "lumen.localhost", "path": "/", "expires": -1, "session": True}
    assert profiles.cookie_param({**session, "size": 2, "sourcePort": 80}) == {
        "name": "s",
        "value": "v",
        "domain": "lumen.localhost",
        "path": "/",
    }
    lasting = {**session, "expires": 1_900_000_000.5, "session": False, "httpOnly": True}
    assert profiles.cookie_param(lasting)["expires"] == 1_900_000_000.5


# A fake login site, and a hub that keeps what the worker sends


class LoginSite:
    """http://<name>.localhost:<port>/: /login sets <name>_session (a token drawn for the site, kept an hour) and goes
    to /home; every request is recorded with the cookies it carried."""

    def __init__(self):
        self.tokens: dict[str, str] = {}
        self.seen: list[tuple[str, str, str]] = []  # (site, path, Cookie header)
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                name = self.headers.get("Host", "").split(".", 1)[0]
                if self.path == "/__seen":
                    body = json.dumps(site.seen).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                else:
                    site.seen.append((name, self.path, self.headers.get("Cookie") or ""))
                    body = f"<p>{name} {self.path}</p>".encode()
                    if self.path == "/login":
                        token = site.tokens.setdefault(name, secrets.token_hex(16))
                        self.send_response(302)
                        self.send_header("Location", "/home")
                        self.send_header("Set-Cookie", f"{name}_session={token}; Max-Age=3600; Path=/; HttpOnly")
                    else:
                        self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, name: str, path: str) -> str:
        return f"http://{name}.localhost:{self.port}{path}"

    def cookies(self, name: str, path: str) -> list[str]:
        return [cookie for site, seen, cookie in self.seen if (site, seen) == (name, path)]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def site():
    found = LoginSite()
    yield found
    found.stop()


class FakeHub:
    """The routes a run's browser identities take, as the hub answers them: each identity's lease, the saves and the
    word that a profile did not open."""

    def __init__(self, identities: dict[int, dict]):
        self.identities = identities
        self.saved: dict[int, tuple[bytes, int, str | None]] = {}
        self.needs_login: list[tuple[int, str]] = []
        self.refuse = False

    async def browser_profile(self, run_id: int, identity_id: int) -> dict:
        if self.refuse:
            raise Refused(f"run {run_id} does not hold browser identity {identity_id}", 409)
        return {"identity_id": identity_id, "status": "active", **self.identities[identity_id]}

    async def save_browser_profile(self, run_id, identity_id, archive, *, base_version, chromium_version) -> dict:
        self.saved[identity_id] = (archive, base_version, chromium_version)
        return {"identity_id": identity_id, "version": base_version + 1}

    async def browser_needs_login(self, run_id: int, identity_id: int, cause: str) -> dict:
        self.needs_login.append((identity_id, cause))
        return {"identity_id": identity_id, "status": "needs_login"}


def identity(name: str, domain: str, *, version: int = 0, archive: bytes | None = None, chromium=None) -> dict:
    profile = None if archive is None else base64.b64encode(archive).decode()
    return {"name": name, "domains": [domain], "version": version, "chromium_version": chromium, "profile": profile}


def daemon_of(home: WorkerHome, hub: FakeHub, installed_chromium: install.Installed) -> types.SimpleNamespace:
    install.write(home, installed_chromium)
    return types.SimpleNamespace(home=home, env=dict(os.environ), browsers={}, hub=hub)


async def cookies_of(session: browser_session.BrowserSession) -> dict[str, str]:
    async with profiles.devtools(session.cdp, session.socket) as tools:
        found = (await tools("Storage.getCookies")).get("cookies") or []
    return {cookie["name"]: cookie["value"] for cookie in found}


async def visit(session: browser_session.BrowserSession, url: str, cookie: str | None = None) -> None:
    """Open ``url`` in a new page of the session's Chromium, and wait for ``cookie`` to be set when one is named."""
    async with profiles.devtools(session.cdp, session.socket) as tools:
        await tools("Target.createTarget", {"url": url})
    deadline = time.monotonic() + LOGIN_WAIT
    while cookie is not None and cookie not in await cookies_of(session):
        assert time.monotonic() < deadline, f"no cookie {cookie} after {url}"
        await asyncio.sleep(0.1)


async def saved_cookies(archive: bytes, where: Path, installed_chromium: install.Installed) -> dict[str, str]:
    """The cookies of the profile ``archive``, as a Chromium started on it reads them."""
    browser_archive.unpack(archive, where)
    async with profiles.own_chromium(
        installed_chromium, where, where.parent / f"{where.name}.log", os.environ
    ) as tools:
        found = (await tools("Storage.getCookies")).get("cookies") or []
    return {cookie["name"]: cookie["value"] for cookie in found}


# The run's Chromium and its identities


def test_browser_profile_each_identity_saves_the_cookies_of_its_domains_and_the_next_run_starts_with_them(
    installed, site, tmp_path
):
    hub = FakeHub(
        {
            11: identity("northwind-admin", "northwind.localhost"),
            12: identity("lumen-ops", "lumen.localhost"),
        }
    )
    daemon = daemon_of(WorkerHome(tmp_path / "worker"), hub, installed)

    async def first_run():
        session = browser_session.BrowserSession(daemon, 7, [11, 12])
        try:
            await session.open()
            for name in ("northwind", "lumen", "elsewhere"):
                await visit(session, site.url(name, "/login"), f"{name}_session")
            await session.save()
            await session.save()  # once
        finally:
            await session.close()

    asyncio.run(first_run())
    assert set(hub.saved) == {11, 12} and hub.needs_login == []
    assert [(base, chromium) for _, base, chromium in hub.saved.values()] == [(0, installed.version)] * 2
    tokens = site.tokens
    northwind = asyncio.run(saved_cookies(hub.saved[11][0], tmp_path / "northwind", installed))
    lumen = asyncio.run(saved_cookies(hub.saved[12][0], tmp_path / "lumen", installed))
    assert northwind == {"northwind_session": tokens["northwind"]}
    assert lumen == {"lumen_session": tokens["lumen"]}
    assert not browser_session.run_directory(daemon.home, 7).exists()

    hub.identities[11].update(identity("northwind-admin", "northwind.localhost", version=1, archive=hub.saved[11][0]))
    hub.identities[12].update(identity("lumen-ops", "lumen.localhost", version=1, archive=hub.saved[12][0]))
    hub.saved.clear()

    async def next_run():
        session = browser_session.BrowserSession(daemon, 8, [11, 12])
        try:
            await session.open()
            found = await cookies_of(session)
            await visit(session, site.url("northwind", "/home"))
            await session.save()
        finally:
            await session.close()
        return found

    found = asyncio.run(next_run())
    assert found == {"northwind_session": tokens["northwind"], "lumen_session": tokens["lumen"]}
    assert [(base, chromium) for _, base, chromium in hub.saved.values()] == [(1, installed.version)] * 2


def test_browser_profile_that_does_not_open_sets_needs_login_and_the_run_starts_empty(installed, tmp_path):
    bad_default = tmp_path / "bad"
    bad_default.mkdir()
    (bad_default / "Default").write_text("not a directory", encoding="utf-8")  # Chromium does not start on it
    hub = FakeHub(
        {
            21: identity(
                "northwind-admin", "northwind.localhost", version=3, archive=browser_archive.pack(bad_default)
            ),
            22: identity("lumen-ops", "lumen.localhost", version=2, archive=b"not a profile"),
        }
    )
    daemon = daemon_of(WorkerHome(tmp_path / "worker"), hub, installed)
    later = str(int(installed.version.split(".")[0]) + 1) + ".0.0.0"

    async def run(ids):
        session = browser_session.BrowserSession(daemon, 9, ids)
        try:
            await session.open()
            notes = session.profiles.take_notes()
            await session.save()
        finally:
            await session.close()
        return notes

    notes = asyncio.run(run([21, 22]))
    assert sorted(hub.needs_login) == [(21, "unopenable"), (22, "unopenable")]
    assert len(notes) == 2 and all("waits for a new login" in note for note in notes)
    assert {key: base for key, (_, base, _) in hub.saved.items()} == {21: 3, 22: 2}  # what it signs into replaces them

    hub.needs_login.clear()
    hub.saved.clear()
    hub.identities[21].update(chromium_version=later, profile=base64.b64encode(b"saved by a later one").decode())
    notes = asyncio.run(run([21]))
    assert hub.needs_login == [(21, "newer_chromium")] and "a later version" in notes[0]
    assert hub.saved == {}  # its profile stays for the Chromium that saved it


def test_browser_profile_the_hub_refuses_fails_the_run_with_cause_environment(installed, tmp_path):
    hub = FakeHub({31: identity("northwind-admin", "northwind.localhost")})
    hub.refuse = True
    daemon = daemon_of(WorkerHome(tmp_path / "worker"), hub, installed)
    session = browser_session.BrowserSession(daemon, 10, [31])
    with pytest.raises(RunFailed) as failed:
        asyncio.run(session.open())
    assert failed.value.fields["cause"] == "environment" and "browser identity 31" in failed.value.error
    assert daemon.browsers == {} and session.process is None
    assert not browser_session.run_directory(daemon.home, 10).exists()


# End to end


BROWSE = """
import json, os, sys, time, urllib.parse, urllib.request
cdp, port, path = os.environ["EVO_BROWSER_CDP"], int(sys.argv[1]), sys.argv[2]
for name in ("northwind", "lumen"):
    url = f"http://{name}.localhost:{port}{path}"
    urllib.request.urlopen(urllib.request.Request(cdp + "/json/new?" + urllib.parse.quote(url, safe=":/"), method="PUT"))
wanted = {("northwind", "/home" if path == "/login" else path), ("lumen", "/home" if path == "/login" else path)}
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    seen = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/__seen"))
    if wanted <= {(site, seen_path) for site, seen_path, _ in seen}:
        sys.exit(0)
    time.sleep(0.1)
sys.exit("the pages did not load")
"""


@pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
def test_browser_profile_a_login_saved_by_a_run_is_there_for_the_next_run_on_the_same_worker(
    make_stack, s3, installed, site
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node on PATH: a run with browser needs it")
    stack = make_stack(secrets_key=KEY, **s3.config())
    os.symlink(node, stack.tmp / "bin" / "node")
    script = stack.tmp / "browse.py"
    script.write_text(BROWSE, encoding="utf-8")
    browse = f"{shlex.quote(sys.executable)} {shlex.quote(str(script))} {site.port}"
    stack.scenarios(
        {
            "2": [
                {"sh": f"{browse} /login"},
                {"result": {"verify_commands": ["true"], "summary": "Signed in to northwind."}},
            ],
            "4": [
                {"sh": f"{browse} /check"},
                {"result": {"verify_commands": ["true"], "summary": "Looked at northwind."}},
            ],
        }
    )
    stack.register()
    install.write(WorkerHome(stack.state), installed)
    proc = stack.start_daemon()
    body = {"name": "northwind-admin", "domains": ["northwind.localhost"], "worker": WORKER, "owner": "member"}

    def created():
        made = stack.client.post(PATH, json=body, headers=stack.owner)
        return made.json() if made.status_code == 201 else None

    made = wait_until(created, "the worker's heartbeat to list the feature browser")

    def listed() -> dict:
        response = stack.client.get(PATH, headers=stack.owner)
        assert response.status_code == 200, response.text
        return next(item for item in response.json() if item["id"] == made["id"])

    first = stack.dispatch([2], identities=["northwind-admin"])[0]
    assert first["browser"] is True and first["browser_identities"] == [made["id"]]
    run = stack.wait_state(first["id"], "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])
    assert site.cookies("northwind", "/home") == [f"northwind_session={site.tokens['northwind']}"]
    saved = listed()
    assert (saved["version"], saved["held_by_run"], saved["chromium_version"]) == (1, None, installed.version)

    second = stack.dispatch([4], identities=["northwind-admin"])[0]
    run = stack.wait_state(second["id"], "done", "failed")
    assert run["state"] == "done", (run["error"], stack.daemon_output()[-6000:])
    assert site.cookies("northwind", "/check") == [f"northwind_session={site.tokens['northwind']}"]  # signed in
    assert site.cookies("lumen", "/check") == [""]  # the first run's cookie of lumen was not saved
    assert listed()["version"] == 2

    finished_cleanly(stack, proc)
    texts = stack.daemon_output() + (stack.state / "worker.log").read_text(encoding="utf-8")
    events = json.dumps([stack.events(first["id"]), stack.events(second["id"])])
    for token in site.tokens.values():
        assert token not in texts and token not in events
