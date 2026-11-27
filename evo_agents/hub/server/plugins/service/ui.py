"""The files of plugins' framed pages and of the hub's UI kit (``routes.ui`` serves them): the cache of bundles'
``pages/`` by SHA-256, the files of core plugins' pages, and the checks a request passes before a file answers."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from importlib import resources

from fastapi import HTTPException

from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.plugin_calls import core_registry
from evo_agents.hub.server.plugins.models.pages import PAGES_DIR
from evo_agents.hub.server.plugins.models.store import IMAGE
from evo_agents.hub.server.plugins.models.ui import (
    KIT_FILE,
    KIT_VERSIONS,
    MAX_CACHED_BYTES,
    NO_PAGE,
    SAFE_PART,
    UI_TYPES,
)
from evo_agents.hub.server.plugins.repository.ui import enabled_version
from evo_agents.hub.server.plugins.service.pages import ticket_live, ticket_ok
from evo_agents.hub.server.plugins.service.store import fetch_bundle
from evo_agents.plugins.bundle import read_bundle


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


async def bundle_pages(blobs: Callable[[], BlobStore], sha256: str, size: int) -> dict[str, bytes]:
    """path below pages/ -> bytes, for the bundle ``sha256`` the hub holds; ``blobs`` gives the blob store, asked only
    when the cache lacks the bundle."""
    files = FILES.get(sha256)
    if files is None:
        data = await fetch_bundle(blobs(), sha256, size)
        files = await asyncio.to_thread(_pages_files, data)
        FILES.put(sha256, files)
    return files


def suffix_of(path: str) -> str:
    """The lowercased suffix of the file ``path`` names, or ''."""
    return path[path.rfind(".") :].lower() if "." in path.rsplit("/", 1)[-1] else ""


async def page_file(state, blobs: Callable[[], BlobStore], plugin: str, project: str, ticket: str, path: str):
    """(bytes, suffix) of the file ``path`` of the pages of plugin ``plugin`` in ``project``; 404 for anything else."""
    secret = state.config.session_secret
    suffix = suffix_of(path)
    if not secret or not ticket_live(ticket) or suffix not in UI_TYPES:  # before the database is asked
        raise HTTPException(404, NO_PAGE)
    async with state.engine.begin() as conn:
        row = await enabled_version(conn, plugin, project)
    core = row is not None and row.tier == "core" and bool((row.manifest or {}).get("ui"))
    if row is None or not (core or (row.tier == "isolated" and row.signature_kind != IMAGE)):
        raise HTTPException(404, NO_PAGE)
    if not ticket_ok(secret, plugin, project, row.sha256, ticket):  # made for the bundle installed now
        raise HTTPException(404, NO_PAGE)
    if core:
        data = await asyncio.to_thread(core_page_file, plugin, path)
    else:
        data = (await bundle_pages(blobs, row.sha256, row.size)).get(path)
    if data is None:
        raise HTTPException(404, NO_PAGE)
    return data, suffix


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


def kit_file(version: str, name: str) -> tuple[bytes, str]:
    """(bytes, suffix) of the file ``name`` of version ``version`` of the hub's UI kit; 404 for anything else."""
    found = KIT_FILE.fullmatch(name)
    if version not in KIT_VERSIONS or found is None:
        raise HTTPException(404, NO_PAGE)
    target = resources.files("evo_agents.hub.server").joinpath("static", "plugin-kit", version, name)
    if not target.is_file():
        raise HTTPException(404, NO_PAGE)
    return target.read_bytes(), "." + found[1]
