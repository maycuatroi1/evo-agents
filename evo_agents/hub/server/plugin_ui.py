"""The files of plugins' framed pages, served to the sandboxed frames of the hub's web, and the hub's UI kit they
load:

    GET /v1/plugin-ui/<plugin>/<project>/<ticket>/<path>
    GET /v1/plugin-ui/kit/1/<file>

``path`` is a file of the installed version's bundle under ``pages/``, or, for a core plugin whose manifest names
``ui`` (module:directory), a file under ``pages/`` of that directory of its package as this process has it; ``evo_agents.hub.server.plugin_pages`` makes the
URL and its ticket, the credential of these public paths (``security.PUBLIC_PREFIXES``), since a frame with an opaque
origin sends no cookie of the hub. A ticket of the wrong shape or run out is refused before the database is asked; its
HMAC is checked against the bundle installed now, so a ticket made before an upgrade frames nothing.

Each file answers with a CSP whose ``sandbox`` keeps the opaque origin even when the URL is opened on its own,
``connect-src 'none'`` so the page fetches nothing itself, only the page's own files (``'self'``) and inline code,
``frame-ancestors 'self'`` so only the hub's web frames it, and no referrer. Only the content types of UI_TYPES are
served; another file of ``pages/`` is a 404. The kit (``static/plugin-kit/1``: evo-ui.css, the hub's tokens in light
and dark with its components, and evo-ui.js, the bridge's client with words in en and vi; ``pnpm build:plugin-kit``
in web/ makes the CSS from the web's tokens) is the same for every plugin and needs no ticket. Bundles are read from the blob store and their ``pages/`` kept in a small
cache by SHA-256, which never goes stale since a version's bytes never change.
"""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from importlib import resources
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request
from fastapi.responses import Response
from sqlalchemy import select

from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import core_registry
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.plugin_pages import PAGES_DIR, UI_PREFIX, ticket_live, ticket_ok
from evo_agents.hub.server.plugin_store import IMAGE, PLUGIN_NAME, fetch_bundle
from evo_agents.plugins.bundle import read_bundle

MAX_CACHED_BYTES = 64 * 1024 * 1024  # the pages of every bundle kept in memory, together
UI_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
UI_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "media-src 'self'",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "frame-ancestors 'self'",
        "sandbox allow-scripts allow-forms",
    )
)
UI_HEADERS = {
    "Content-Security-Policy": UI_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, max-age=300",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
NO_PAGE = "no such page of a plugin here: open it from the project's menu again"
KIT_VERSIONS = ("1",)
KIT_FILE = re.compile(r"[a-z][a-z0-9-]{0,60}\.(css|js)")
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}")  # a part of a path below pages/: no .., no hidden file
KIT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=3600",
    "Access-Control-Allow-Origin": "*",  # a sandboxed frame's origin is opaque: null
    "Cross-Origin-Resource-Policy": "cross-origin",
}
PluginName = Annotated[str, Path(pattern=PLUGIN_NAME)]

router = APIRouter(prefix=UI_PREFIX.rstrip("/"), tags=["plugins"], include_in_schema=False)


class _Files:
    """The ``pages/`` files of bundles by SHA-256, the most recently used kept within MAX_CACHED_BYTES."""

    def __init__(self, limit: int = MAX_CACHED_BYTES):
        self.limit = limit
        self.held: OrderedDict[str, dict[str, bytes]] = OrderedDict()

    def get(self, sha256: str) -> dict[str, bytes] | None:
        files = self.held.get(sha256)
        if files is not None:
            self.held.move_to_end(sha256)
        return files

    def put(self, sha256: str, files: dict[str, bytes]) -> None:
        self.held[sha256] = files
        self.held.move_to_end(sha256)
        while len(self.held) > 1 and sum(map(_size, self.held.values())) > self.limit:
            self.held.popitem(last=False)


def _size(files: dict[str, bytes]) -> int:
    return sum(len(data) for data in files.values())


FILES = _Files()


def _pages_files(data: bytes) -> dict[str, bytes]:
    contents = read_bundle(data)
    return {entry.path[len(PAGES_DIR) :]: entry.data for entry in contents.files if entry.path.startswith(PAGES_DIR)}


async def bundle_pages(request: Request, sha256: str, size: int) -> dict[str, bytes]:
    """path below pages/ -> bytes, for the bundle ``sha256`` the hub holds."""
    files = FILES.get(sha256)
    if files is None:
        data = await fetch_bundle(blob_store(request), sha256, size)
        files = await asyncio.to_thread(_pages_files, data)
        FILES.put(sha256, files)
    return files


def _enabled_version():
    p, v, pp, pr = tables.plugins, tables.plugin_versions, tables.project_plugins, tables.projects
    return (
        select(v.c.sha256, v.c.size, v.c.tier, v.c.signature_kind, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .join(pp, pp.c.plugin_id == p.c.id)
        .join(pr, pr.c.id == pp.c.project_id)
        .where(p.c.disabled_reason.is_(None))
    )


@router.get("/{plugin}/{project}/{ticket}/{path:path}")
async def ui_file(request: Request, plugin: PluginName, project: ProjectName, ticket: str, path: str) -> Response:
    """A file of an isolated plugin's pages, for the sandboxed frame of the web; 404 for anything else."""
    secret = request.app.state.config.session_secret
    suffix = path[path.rfind(".") :].lower() if "." in path.rsplit("/", 1)[-1] else ""
    if not secret or not ticket_live(ticket) or suffix not in UI_TYPES:  # before the database is asked
        raise HTTPException(404, NO_PAGE)
    query = _enabled_version().where(tables.plugins.c.name == plugin, tables.projects.c.name == project)
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(query)).first()
    core = row is not None and row.tier == "core" and bool((row.manifest or {}).get("ui"))
    if row is None or not (core or (row.tier == "isolated" and row.signature_kind != IMAGE)):
        raise HTTPException(404, NO_PAGE)
    if not ticket_ok(secret, plugin, project, row.sha256, ticket):  # made for the bundle installed now
        raise HTTPException(404, NO_PAGE)
    if core:
        data = await asyncio.to_thread(core_page_file, plugin, path)
    else:
        data = (await bundle_pages(request, row.sha256, row.size)).get(path)
    if data is None:
        raise HTTPException(404, NO_PAGE)
    return Response(data, media_type=UI_TYPES[suffix], headers=UI_HEADERS)


def core_page_file(plugin: str, path: str) -> bytes | None:
    """The bytes of ``path`` below ``pages/`` of the ``ui`` directory of core plugin ``plugin`` as this process has
    it installed, or None: a path with a part that is empty, hidden or .. names no file."""
    parts = path.split("/")
    if not all(SAFE_PART.fullmatch(part) and part not in (".", "..") for part in parts):
        return None
    found = next((item for item in core_registry().plugins if item.manifest.id == plugin), None)
    ui = found.manifest.ui if found is not None else None
    if not ui:
        return None
    package, _, directory = ui.partition(":")
    try:
        target = resources.files(package).joinpath(*directory.split("/"), "pages", *parts)
        return target.read_bytes() if target.is_file() else None
    except (ModuleNotFoundError, OSError):
        return None


@router.get("/kit/{version}/{name}")
async def kit_file(version: str, name: str) -> Response:
    """A file of the hub's UI kit for plugins' pages, version ``version``: evo-ui.css or evo-ui.js."""
    found = KIT_FILE.fullmatch(name)
    if version not in KIT_VERSIONS or found is None:
        raise HTTPException(404, NO_PAGE)
    target = resources.files("evo_agents.hub.server").joinpath("static", "plugin-kit", version, name)
    if not target.is_file():
        raise HTTPException(404, NO_PAGE)
    return Response(target.read_bytes(), media_type=UI_TYPES["." + found[1]], headers=KIT_HEADERS)
