"""The files of plugins' framed pages, served to the sandboxed frames of the hub's web, and the hub's UI kit they
load:

    GET /v1/plugin-ui/<plugin>/<project>/<ticket>/<path>
    GET /v1/plugin-ui/kit/1/<file>

``path`` is a file of the installed version's bundle under ``pages/``, or, for a core plugin whose manifest names
``ui`` (module:directory), a file under ``pages/`` of that directory of its package as this process has it; ``evo_agents.hub.server.plugins.service.pages`` makes the
URL and its ticket, the credential of these public paths (``security.PUBLIC_PREFIXES``), since a frame with an opaque
origin sends no cookie of the hub. A ticket of the wrong shape or run out is refused before the database is asked; its
HMAC is checked against the bundle installed now, so a ticket made before an upgrade frames nothing.

Each file answers with a CSP whose ``sandbox`` keeps the opaque origin even when the URL is opened on its own,
``connect-src 'none'`` so the page fetches nothing itself, only the page's own files (``'self'``) and inline code,
``frame-ancestors 'self'`` so only the hub's web frames it, and no referrer. Only the content types of UI_TYPES are
served; another file of ``pages/`` is a 404. The kit (``static/plugin-kit/1``: evo-ui.css, the hub's tokens in light
and dark with its components, and evo-ui.js, the bridge's client with words in en and vi; ``pnpm build:plugin-kit``
in web/ makes the CSS from the web's tokens) is the same for every plugin and needs no ticket. Bundles are read from the blob store and their ``pages/`` kept in a small
cache by SHA-256 (``service.ui``), which never goes stale since a version's bytes never change.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.blobs import blob_store
from evo_agents.hub.server.plugins.models.pages import UI_PREFIX
from evo_agents.hub.server.plugins.models.ui import KIT_HEADERS, UI_HEADERS, UI_TYPES, PluginName
from evo_agents.hub.server.plugins.service import ui as files

router = APIRouter(prefix=UI_PREFIX.rstrip("/"), tags=["plugins"], include_in_schema=False)


@router.get("/{plugin}/{project}/{ticket}/{path:path}")
async def ui_file(request: Request, plugin: PluginName, project: ProjectName, ticket: str, path: str) -> Response:
    """A file of an isolated plugin's pages, for the sandboxed frame of the web; 404 for anything else."""
    state = request.app.state
    data, suffix = await files.page_file(state, lambda: blob_store(request), plugin, project, ticket, path)
    return Response(data, media_type=UI_TYPES[suffix], headers=UI_HEADERS)


@router.get("/kit/{version}/{name}")
async def kit_file(version: str, name: str) -> Response:
    """A file of the hub's UI kit for plugins' pages, version ``version``: evo-ui.css or evo-ui.js."""
    data, suffix = files.kit_file(version, name)
    return Response(data, media_type=UI_TYPES[suffix], headers=KIT_HEADERS)
