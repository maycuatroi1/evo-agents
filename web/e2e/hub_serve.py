"""``evo-agents hub serve`` for the Playwright stack (hub_stack.py), with the tests' core plugin notes in its image.

notes (tests/hub/core_notes.py) is a core plugin with a table of its own, routes, an MCP tool and a native page of
the web (web/src/components/plugin-notes, a route only in the end-to-end build). A hub's image would carry it as a
package that declares the entry point and puts its table's revision in the hub's chain of migrations; here the
launcher adds both to this process before the hub starts, as tests/hub/test_plugin_points.py does in its own:
the entry point to the group the hub reads (``evo_agents.plugins.core``), and the revision of
tests/hub/core_notes_migrations after the hub's head. The hub then migrates the table and records the plugin in its
store as one of its image, which a project's admin enables.

The hub's provider accounts call the endpoint a member gives (Test connection, Fetch models) after checking that its
host resolves to public addresses only. Here no call leaves the machine: every host resolves to one public address of
the documentation range, and the endpoint is a fake one (``fake_endpoint``) that lists FAKE_MODELS to a key starting
with GOOD_KEY and answers 401 to any other.

Arguments are those of ``evo-agents hub serve``.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import EntryPoint
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]  # the evo-agents checkout this web/ belongs to
sys.path.insert(0, str(ROOT))

from evo_agents.cli import main  # noqa: E402
from evo_agents.hub import migrate as hub_migrate  # noqa: E402
from evo_agents.hub.server import app as hub_app  # noqa: E402
from evo_agents.plugins import core  # noqa: E402
from tests.hub import core_notes  # noqa: E402

NOTES = EntryPoint("notes", "tests.hub.core_notes:MANIFEST", core.ENTRY_POINT_GROUP)
installed = core.entry_points
hub_config = hub_migrate.alembic_config


def with_notes(**selection):
    """The entry points the environment declares, notes added to the group of core plugins."""
    found = list(installed(**selection))
    return [*found, NOTES] if selection.get("group") == core.ENTRY_POINT_GROUP else found


def alembic_config():
    """The hub's chain of migrations with the revision of notes appended after its head."""
    config = hub_config()
    versions = hub_migrate.script_location() / "versions"
    config.set_main_option("path_separator", "os")
    config.set_main_option("version_locations", os.pathsep.join((str(versions), str(core_notes.MIGRATIONS))))
    return config


PUBLIC_ADDRESS = "93.184.216.34"
GOOD_KEY = "e2e-good-"  # web/e2e/support/providers.ts
FAKE_MODELS = ("fake-large", "fake-small", "fake-coder")
created = hub_app.create_app


async def public_resolver(host: str, port: int) -> list[str]:
    """Every host is one public address: the hub's check passes and the fake endpoint answers."""
    return [PUBLIC_ADDRESS]


def fake_endpoint(request: httpx.Request) -> httpx.Response:
    """GET .../models lists FAKE_MODELS to a key starting with GOOD_KEY, by any of the formats' headers; else 401."""
    sent = request.headers.get("authorization", "").removeprefix("Bearer ")
    key = sent or request.headers.get("x-api-key", "") or request.headers.get("x-goog-api-key", "")
    if not key.startswith(GOOD_KEY):
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})
    if request.method == "GET" and request.url.path.endswith("/models"):
        return httpx.Response(200, json={"object": "list", "data": [{"id": name} for name in FAKE_MODELS]})
    return httpx.Response(200, json={"id": "fake", "choices": []})


def with_fake_endpoint(config):
    """The hub, its provider accounts calling the fake endpoint through a resolver that says every host is public."""
    app = created(config)
    app.state.provider_resolver = public_resolver
    app.state.provider_transport = httpx.MockTransport(fake_endpoint)
    return app


core.entry_points = with_notes
hub_migrate.alembic_config = alembic_config
hub_app.create_app = with_fake_endpoint

if __name__ == "__main__":
    raise SystemExit(main(["hub", "serve", *sys.argv[1:]]))
