"""The hub's reading of plugin markets (plan plugin-market, criterion a5): the index of each market source and the
latest release of each of its free isolated plugins, from GitHub, kept in ``market_sources``.

``refresh`` reads every source (or one), each on its own: the index through the contents API with the ETag of the
last read, so an index that did not change costs a 304 and keeps the cached one; then the latest release of each free
isolated plugin the index lists, whose bundle it downloads only when the tag changed since the last refresh, to read
the version and the permissions its manifest asks for. A source that fails writes ``last_error`` and keeps its last
index and releases; one plugin whose release does not read keeps ``{tag?, error}`` in ``releases``; neither stops the
others. Nothing a refresh reads is trusted: an install downloads the release again and checks its signature
(``evo_agents.hub.server.market``).

The first refresh adds the market the operator names in EVO_HUB_MARKET_DEFAULT_SOURCE as the ``builtin`` source, moves
it when the variable names another repo, and drops it when the variable is empty. The job ``hub.market_refresh`` runs
it every six hours (``evo_agents.hub.worker``); a hub admin's refresh button runs it for one source.

``GitHubMarket`` is the HTTP of it: GitHub's API at EVO_HUB_GITHUB_API_URL with EVO_HUB_GITHUB_TIMEOUT per call, the
source's token, when it has one, in a header only, and an asset's download following GitHub's redirect to its storage
without the token. It is the ``evo_agents.plugins.market.Releases`` an install reads releases through too.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents import __version__
from evo_agents.hub import tables
from evo_agents.hub.config import HubConfig
from evo_agents.plugins.bundle import MAX_BUNDLE, BundleError, read_bundle
from evo_agents.plugins.market import MAX_MARKET, MarketError, ReleaseError, read_market, release_assets
from evo_agents.plugins.signing import MAX_SIGNATURE

log = logging.getLogger(__name__)

API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
MAX_ERROR_CHARS = 2000  # market_sources.last_error (0149)
MAX_RELEASE = 1024 * 1024  # bytes of a release as the API describes it


class MarketUnavailable(Exception):
    """GitHub did not answer, or answered what the hub cannot use; the message is safe to show."""


class Sealer(Protocol):
    def open_stored(self, stored: dict, aad: bytes) -> str: ...


def token_aad(source_id: int) -> bytes:
    """The associated data of a source's sealed token (``evo_agents.hub.server.sealing.market_source_aad``)."""
    return f"market-source:{source_id}".encode()


@dataclass(frozen=True)
class Answer:
    status: int
    headers: httpx.Headers
    body: bytes

    def json(self):
        return json.loads(self.body)


@dataclass(frozen=True)
class Index:
    data: bytes
    sha: str | None
    etag: str | None


class GitHubMarket:
    """The contents and releases of GitHub repos, as the hub reads them for its markets."""

    def __init__(self, config: HubConfig, token: str | None = None, transport: httpx.BaseTransport | None = None):
        self.api = config.github_api_url.rstrip("/")
        headers = {"User-Agent": f"evo-agents-hub/{__version__}", **API_HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(timeout=httpx.Timeout(config.github_timeout), headers=headers, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubMarket:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, url: str, doing: str, *, limit: int, **kwargs) -> Answer | None:
        """The answer to a GET, read up to ``limit`` bytes; None for a 404."""
        try:
            with self._http.stream("GET", url, **kwargs) as response:
                if response.status_code == 404:
                    return None
                if response.status_code == 304:
                    return Answer(304, response.headers, b"")
                if response.status_code >= 400:
                    raise MarketUnavailable(f"GitHub answered {response.status_code} while {doing}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise MarketUnavailable(f"GitHub answered more than {limit} bytes while {doing}")
                return Answer(response.status_code, response.headers, bytes(body))
        except httpx.TimeoutException:
            raise MarketUnavailable(f"GitHub did not answer in time while {doing}") from None
        except httpx.HTTPError as exc:
            raise MarketUnavailable(f"GitHub could not be reached while {doing}: {type(exc).__name__}") from None

    def contents(self, repo: str, path: str, ref: str, etag: str | None = None) -> Index | None:
        """The file ``path`` of ``repo`` at ``ref``; None when it did not change since ``etag``."""
        doing = f"reading {path} of {repo} at {ref}"
        headers = {"If-None-Match": etag} if etag else {}
        url = f"{self.api}/repos/{repo}/contents/{path}"
        response = self._get(url, doing, limit=2 * MAX_MARKET, params={"ref": ref}, headers=headers)
        if response is None:
            raise MarketUnavailable(f"{repo} has no {path} at {ref}, or the hub may not read it")
        if response.status == 304:
            return None
        try:
            found = response.json()
            data = base64.b64decode(found["content"])
        except (ValueError, KeyError, TypeError):
            raise MarketUnavailable(f"GitHub did not give the contents of {path} while {doing}") from None
        return Index(data, found.get("sha"), response.headers.get("ETag"))

    def release(self, repo: str, tag: str | None) -> dict | None:
        """The release of ``repo`` tagged ``tag``, or its latest; None when it has none."""
        where = f"tags/{tag}" if tag else "latest"
        response = self._get(
            f"{self.api}/repos/{repo}/releases/{where}", f"reading the releases of {repo}", limit=MAX_RELEASE
        )
        if response is None:
            return None
        try:
            found = response.json()
        except ValueError:
            raise MarketUnavailable(f"GitHub did not describe a release of {repo}") from None
        return found if isinstance(found, dict) else None

    def download(self, asset: dict) -> bytes:
        """The bytes of release asset ``asset``, through GitHub's redirect to its storage (httpx leaves the token out of
        a redirect to another host)."""
        name = str(asset.get("name"))
        limit = MAX_SIGNATURE if name.endswith(".json") else MAX_BUNDLE
        response = self._get(
            str(asset.get("url")),
            f"downloading {name}",
            limit=limit,
            headers={"Accept": "application/octet-stream"},
            follow_redirects=True,
        )
        if response is None:
            raise MarketUnavailable(f"GitHub has no asset {name}")
        return response.body


# Reading a source


def _release_of(reader: GitHubMarket, item: dict, known: dict | None) -> dict:
    """What the market keeps of the latest release of ``item``: ``{tag, version, permissions}`` or ``{error}``."""
    release = reader.release(item["repo"], None)
    if release is None:
        return {"error": f"{item['repo']} has no release"}
    try:
        assets = release_assets(item["id"], release)
    except ReleaseError as exc:
        return {"tag": release.get("tag_name"), "error": str(exc)}
    if known and known.get("tag") == assets.tag and "error" not in known:
        return known
    try:
        manifest = read_bundle(reader.download(assets.bundle)).manifest
    except BundleError as exc:
        return {"tag": assets.tag, "error": f"{assets.bundle.get('name')}: {exc}"}
    if manifest.id != item["id"] or manifest.version != assets.version:
        return {"tag": assets.tag, "error": f"the bundle of {assets.tag} is {manifest.id} {manifest.version}"}
    return {"tag": assets.tag, "version": manifest.version, "permissions": list(manifest.permissions)}


def read_source(reader: GitHubMarket, row) -> dict:
    """The columns a refresh of source ``row`` writes; MarketUnavailable or MarketError when its index does not read."""
    index = reader.contents(row.repo, row.path, row.ref, row.etag if row.index is not None else None)
    values: dict = {"last_error": None}
    if index is None:
        market = row.index
    else:
        market = read_market(index.data)
        values.update(index=market, etag=index.etag, fetched_sha=index.sha)
    known = row.releases or {}
    releases = {}
    for item in market["plugins"]:
        if item["tier"] != "isolated" or item["price"]["kind"] != "free":
            continue
        try:
            releases[item["id"]] = _release_of(reader, item, known.get(item["id"]))
        except MarketUnavailable as exc:
            releases[item["id"]] = {**(known.get(item["id"]) or {}), "error": str(exc)}
    values["releases"] = releases
    return values


def _problem(exc: Exception) -> str:
    if isinstance(exc, MarketError):
        text = "the index is not plugin-market-v1: " + "; ".join(exc.problems)
    else:
        text = str(exc)
    return text[:MAX_ERROR_CHARS]


# The sources


async def place_builtin(engine: AsyncEngine, config: HubConfig) -> None:
    """The operator's market as the builtin source: added, moved to another repo, or dropped with the variable."""
    sources = tables.market_sources
    repo = config.market_default_source
    async with engine.begin() as conn:
        held = (await conn.execute(select(sources).where(sources.c.builtin))).first()
        if repo is None:
            if held is not None:
                await conn.execute(delete(sources).where(sources.c.id == held.id))
            return
        if held is None:
            await conn.execute(pg_insert(sources).values(repo=repo, builtin=True).on_conflict_do_nothing())
        elif held.repo != repo:
            cleared = {"index": None, "etag": None, "fetched_sha": None, "releases": {}, "last_error": None}
            await conn.execute(update(sources).where(sources.c.id == held.id).values(repo=repo, **cleared))


def _token(row, sealer: Sealer | None) -> str | None:
    if row.token is None:
        return None
    if sealer is None:
        raise MarketUnavailable("the source's token is sealed and this hub has no EVO_HUB_SECRETS_KEY to open it")
    try:
        return sealer.open_stored(row.token, token_aad(row.id))
    except Exception:  # Unsealable, whose module this one may not import
        raise MarketUnavailable("the source's token does not open with this hub's key: set it again") from None


def _read(config: HubConfig, row, sealer: Sealer | None, transport) -> dict:
    try:
        with GitHubMarket(config, _token(row, sealer), transport) as reader:
            return read_source(reader, row)
    except (MarketUnavailable, MarketError) as exc:
        return {"last_error": _problem(exc)}


async def refresh(
    engine: AsyncEngine,
    config: HubConfig,
    sealer: Sealer | None,
    *,
    source_id: int | None = None,
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """Read every market source, or source ``source_id`` alone; for each, its id, repo and the error it ended with."""
    if source_id is None:
        await place_builtin(engine, config)
    sources = tables.market_sources
    query = select(sources).order_by(sources.c.id)
    if source_id is not None:
        query = query.where(sources.c.id == source_id)
    async with engine.begin() as conn:
        rows = (await conn.execute(query)).all()
    done = []
    for row in rows:
        values = await asyncio.to_thread(_read, config, row, sealer, transport)
        async with engine.begin() as conn:
            await conn.execute(update(sources).where(sources.c.id == row.id).values(fetched_at=func.now(), **values))
        done.append({"id": row.id, "repo": row.repo, "error": values.get("last_error")})
        log.info("market refreshed", extra={"source": row.repo, "failed": values.get("last_error") is not None})
    return done
