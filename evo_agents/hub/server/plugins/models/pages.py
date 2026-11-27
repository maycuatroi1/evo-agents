"""The shapes of the pages plugins add to a project's part of the web (``service.pages``), and the constants of their
tickets."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

UI_PREFIX = "/v1/plugin-ui/"
PAGES_DIR = "pages/"
DEFAULT_ICON = "puzzle"
TICKET_TTL = 12 * 3600  # seconds a ticket lasts past the hour it was made in
TICKET_STEP = 3600  # tickets made within one hour are the same
TICKET = re.compile(r"([0-9]{1,12})\.([0-9a-f]{32})")  # <expires>.<mac>
PURPOSE = "plugin-ui"  # of the ticket's HMAC under the session secret (security.keyed_digest)
# The areas of a project's menu a page's item may sit in (plugin.schema.json, $defs.page.area): the Business cycle,
# the Development cycle and the folds after them. A value the manifest does not give, or one this release does not
# know (a manifest stored by a release that did not check it), is none, which the web shows in apps.
AREAS = ("business", "development", "improvement", "knowledge", "apps")


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
    slot: str | None = Field(
        default=None,
        description="where the web shows the page instead of on a page of its own: decide, a panel of the project's "
        "Decisions page; null for a page of its own",
    )
    area: str | None = Field(
        default=None,
        description="the area of the project's menu its item sits in: business (the Business cycle), development "
        "(the Development cycle), improvement, knowledge or apps; null when the manifest names none, which the web "
        "shows in apps",
    )
