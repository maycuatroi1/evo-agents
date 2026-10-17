"""Pages of plugins on the hub's web (extension point ``pages``, version 1): what a project's menu lists, and the
tickets of the URLs that frame an isolated plugin's pages (``evo_agents.hub.server.plugin_ui`` serves their files).

A page lives at /p/<project>/x/<plugin>/<page> on the web, in a project that enabled the plugin. A core plugin's page
is native, a page of the web itself (web/src), so the hub only says it exists (``native``). An isolated plugin's page
is an HTML file of its bundle under ``pages/``; the web frames ``url``, which ``plugin_ui`` serves:

    GET /v1/plugin-ui/<plugin>/<project>/<ticket>/<path>

``path`` is a file of the installed version's bundle under ``pages/`` (``pages/board.html`` is ``board.html``), so the
page loads its scripts, styles and images by relative URL. The frame is sandboxed without ``allow-same-origin``: its
document has an opaque origin, its requests carry no cookie of the hub, and it reaches the hub only through the bridge
of the web (docs/plugins.md, Pages). The ticket is therefore the credential, and these paths are public
(``security.PUBLIC_PREFIXES``): ``<expires>.<mac>``, an HMAC of the plugin, the project and the expiry under the
hub's session secret, handed out with the project's plugins (GET /v1/projects/{p}/plugins) to whoever may read them.
It lasts 12 hours past the hour it was made in, so a menu read twice in an hour frames the same URL. Every file is
checked again as it is served: the plugin installed, turned on and enabled in the project; anything else, a ticket
that is wrong or old included, is the same 404, which says nothing of what exists.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time

from pydantic import BaseModel, Field

UI_PREFIX = "/v1/plugin-ui/"
PAGES_DIR = "pages/"
DEFAULT_ICON = "puzzle"
TICKET_TTL = 12 * 3600  # seconds a ticket lasts past the hour it was made in
TICKET_STEP = 3600  # tickets made within one hour are the same
TICKET = re.compile(r"([0-9]{1,12})\.([0-9a-f]{32})")  # <expires>.<mac>


class PluginPage(BaseModel):
    """A page a plugin adds to a project's part of the web."""

    name: str
    title: str = Field(description="its name in the project's menu and above the page")
    description: str
    icon: str = Field(description="the icon of its menu item, a name from the hub's set")
    menu: bool = Field(description="whether the project's menu lists it")
    native: bool = Field(description="a page of the hub's web itself (a core plugin's); false for a framed page")
    url: str | None = Field(
        description="what the web frames for an isolated plugin's page, a ticket inside it: only in a project that "
        "enabled the plugin, null otherwise and for a native page"
    )


# Tickets


def _mac(secret: str, plugin: str, project: str, expires: int) -> str:
    message = f"plugin-ui\n{plugin}\n{project}\n{expires}".encode()
    digest = hmac.new(secret.encode(), message, hashlib.sha256).digest()
    return digest.hex()[:32]


def ticket(secret: str, plugin: str, project: str, now: float | None = None) -> str:
    """The ticket of the pages of ``plugin`` in ``project``: the same for every call within one hour."""
    moment = int(time.time() if now is None else now)
    expires = moment - moment % TICKET_STEP + TICKET_STEP + TICKET_TTL
    return f"{expires}.{_mac(secret, plugin, project, expires)}"


def ticket_ok(secret: str | None, plugin: str, project: str, given: str, now: float | None = None) -> bool:
    """Whether ``given`` is a ticket of ``plugin`` in ``project`` that has not run out; never without a secret."""
    found = TICKET.fullmatch(given)
    if not secret or found is None:
        return False
    expires, mac = int(found[1]), found[2]
    if expires <= (time.time() if now is None else now):
        return False
    return hmac.compare_digest(mac, _mac(secret, plugin, project, expires))


def page_url(secret: str, plugin: str, project: str, entry: str, now: float | None = None) -> str:
    """Where the web frames the page whose HTML file is ``entry`` (pages/...), relative to the hub's origin."""
    return f"{UI_PREFIX}{plugin}/{project}/{ticket(secret, plugin, project, now)}/{entry[len(PAGES_DIR) :]}"


def pages_of(manifest: dict, plugin: str, project: str, enabled: bool, secret: str | None) -> list[PluginPage]:
    """The pages of the plugin whose installed manifest is ``manifest``, as project ``project`` sees them. A hub
    without a session secret (EVO_HUB_SESSION_SECRET, which the web's sign-in needs too) signs no ticket, so its
    framed pages have no URL."""
    isolated = manifest.get("tier", "isolated") == "isolated"
    found = []
    for item in (manifest.get("contributes") or {}).get("pages") or []:
        entry = item.get("entry")
        found.append(
            PluginPage(
                name=item["name"],
                title=item.get("title") or manifest.get("name") or plugin,
                description=item.get("description", ""),
                icon=item.get("icon", DEFAULT_ICON),
                menu=item.get("menu", True),
                native=not isolated,
                url=page_url(secret, plugin, project, entry) if isolated and enabled and entry and secret else None,
            )
        )
    return found
