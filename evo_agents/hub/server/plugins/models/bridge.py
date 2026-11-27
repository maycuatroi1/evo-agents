"""The constants of the hub's check of what a plugin's page asks through the bridge of the web (``routes.bridge``)."""

from __future__ import annotations

import re

from evo_agents.plugins.manifest import PLUGIN_ID

PLUGIN_HEADER = "X-Evo-Plugin"
PLUGIN_ROUTES = "/v1/x/"  # extension.MOUNT, the routes of plugins themselves
PLUGIN_PATTERN = re.compile(PLUGIN_ID)
