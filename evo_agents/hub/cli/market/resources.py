"""The routes of the hub ``evo-agents hub market`` and ``hub plugin publisher`` ask, one gateway each, and the keys of
their answers that the commands declare for ``--json`` (the contract, ``evo-agents hub contract print``).

- :class:`Market`: GET /v1/market and /v1/market/{source}/{name}, and POST .../install, .../request and .../buy.
- :class:`MarketSources`: GET and POST /v1/market/sources, DELETE .../{id} and POST .../{id}/refresh.
- :class:`Licenses`: GET /v1/market/licenses, POST .../licenses/check and GET /v1/market/hub.
- :class:`GithubInstalls`: POST /v1/plugins/install-github, and :class:`ProjectPlugins` the same route of a project.
- :class:`Seller`: GET /v1/seller, its listings, bundles, orders, revenue and payouts.
- :class:`Publishers`: GET and POST /v1/plugin-publishers and DELETE .../{id}.

These routes belong to the hub, not to a project, so the gateways take the hub alone (as
``evo_agents.hub.cli.stop.AutonomyStops`` does). A plugin's name and a market's id go into the path as they are given.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from evo_agents.hub.cli.base import ProjectResource, call_object
from evo_agents.hub.client import Hub, HubError

LIST_KEYS = ("plugins", "industries", "categories", "markets")
PLUGIN_KEYS = (
    "source",
    "market",
    "id",
    "name",
    "description",
    "industries",
    "categories",
    "repo",
    "publisher",
    "tier",
    "package",
    "price",
    "icon",
    "homepage",
    "license",
    "requires",
    "status",
    "installed_version",
    "latest",
)
DETAIL_KEYS = (*PLUGIN_KEYS, "trusted", "may_trust")
SOURCE_KEYS = (
    "id",
    "repo",
    "name",
    "title",
    "fetched_at",
    "error",
    "ref",
    "path",
    "builtin",
    "private",
    "added_by",
    "added_at",
    "plugins",
)
SOURCES_KEYS = ("sources", "default_source", "publishers_locked")
INSTALLED_KEYS = ("plugin", "trusted_publisher", "upgraded")
REQUEST_KEYS = ("id", "project", "plugin", "state", "created")
PUBLISHER_KEYS = ("id", "identities", "core", "operator", "trusted")
SELLER_KEYS = ("seller", "key_id", "public_key", "selling", "payment_instructions", "signal_project")
LISTING_KEYS = (
    "plugin",
    "publisher",
    "signer",
    "owner",
    "usd_per_hub_month",
    "state",
    "bundles",
    "active_licenses",
    "created_at",
    "updated_at",
)
ORDER_KEYS = (
    "number",
    "plugin",
    "publisher",
    "buyer_hub",
    "buyer_url",
    "buyer_name",
    "buyer_email",
    "months",
    "usd_per_month",
    "usd_total",
    "state",
    "payment",
    "created_at",
    "invoice",
    "closed_by",
    "closed_at",
    "license_id",
    "valid_from",
    "valid_until",
)
CHECKED_KEYS = ("checked", "received", "installed", "reminded", "lapsed", "errors")
REVENUE_KEYS = ("month", "publishers", "gross_usd", "publisher_usd", "platform_usd")


def with_query(path: str, **query: Any) -> str:
    """``path?<query>``, a query value of None left out; ``path`` alone when none is left."""
    given = urlencode({name: value for name, value in query.items() if value is not None})
    return path + (f"?{given}" if given else "")


class HubRoutes:
    """A gateway to routes of the hub that belong to no project."""

    def __init__(self, hub: Hub):
        self.hub = hub

    def answer(self, method: str, path: str, body: dict | None = None) -> dict:
        """The hub's answer to ``method path``, a JSON object; HubError when it is not."""
        return call_object(self.hub, method, path, body)


class Market(HubRoutes):
    """The plugins of every market the hub reads (Gateway)."""

    path = "/v1/market"

    def plugins(self, **query: str | None) -> dict:
        return self.answer("GET", with_query(self.path, **query))

    def source_of(self, name: str, source: int | None) -> int:
        """The source that lists plugin ``name``: ``source``, or the only one that does."""
        if source is not None:
            return source
        sources = sorted({item["source"] for item in self.plugins()["plugins"] if item["id"] == name})
        if not sources:
            raise HubError(f"no market of the hub lists a plugin {name}: see `evo-agents hub market list`")
        if len(sources) > 1:
            listed = ", ".join(str(item) for item in sources)
            raise HubError(f"markets {listed} list a plugin {name}: name one with --source")
        return sources[0]

    def plugin_path(self, source: int, name: str, action: str | None = None) -> str:
        return f"{self.path}/{source}/{name}" + (f"/{action}" if action else "")

    def plugin(self, source: int, name: str) -> dict:
        return self.answer("GET", self.plugin_path(source, name))

    def post(self, source: int, name: str, action: str, body: dict) -> dict:
        """POST .../install, .../request or .../buy of one plugin."""
        return self.answer("POST", self.plugin_path(source, name, action), body)


class MarketSources(HubRoutes):
    """The markets the hub reads (Gateway)."""

    path = "/v1/market/sources"

    def list(self) -> dict:
        return self.answer("GET", self.path)

    def add(self, body: dict) -> dict:
        return self.answer("POST", self.path, body)

    def remove(self, source: int) -> dict:
        return self.answer("DELETE", f"{self.path}/{source}")

    def refresh(self, source: int) -> dict:
        return self.answer("POST", f"{self.path}/{source}/refresh")


class Licenses(HubRoutes):
    """The hub's orders of paid plugins, their licences, and the id a licence names (Gateway)."""

    path = "/v1/market/licenses"

    def list(self) -> dict:
        return self.answer("GET", self.path)

    def check(self) -> dict:
        return self.answer("POST", f"{self.path}/check")

    def hub_id(self) -> dict:
        return self.answer("GET", "/v1/market/hub")


class GithubInstalls(HubRoutes):
    """Installing a plugin for the whole hub from a GitHub release (Gateway)."""

    path = "/v1/plugins/install-github"

    def post(self, body: dict) -> dict:
        return self.answer("POST", self.path, body)


class ProjectPlugins(ProjectResource):
    """Installing a plugin from a GitHub release for one self-managed project alone (Gateway)."""

    collection = "plugins"

    def install_github(self, body: dict) -> dict:
        return self.call("POST", "install-github", body=body)


class Seller(HubRoutes):
    """What the hub sells, as the hub feature ``seller`` (Gateway)."""

    path = "/v1/seller"

    def info(self) -> dict:
        return self.answer("GET", self.path)

    def listings(self) -> dict:
        return self.answer("GET", f"{self.path}/listings")

    def add_listing(self, body: dict) -> dict:
        return self.answer("POST", f"{self.path}/listings", body)

    def change_listing(self, plugin: str, body: dict) -> dict:
        return self.answer("PATCH", f"{self.path}/listings/{plugin}", body)

    def upload_bundle(self, plugin: str, body: dict) -> dict:
        return self.answer("POST", f"{self.path}/listings/{plugin}/bundles", body)

    def orders(self, state: str | None) -> dict:
        return self.answer("GET", with_query(f"{self.path}/orders", state=state or None))

    def close_order(self, number: str, action: str, body: dict | None = None) -> dict:
        """POST .../orders/{number}/paid or .../cancel."""
        return self.answer("POST", f"{self.path}/orders/{number}/{action}", body)

    def revenue(self, month: str | None) -> dict:
        return self.answer("GET", with_query(f"{self.path}/revenue", month=month or None))

    def payout(self, body: dict) -> dict:
        return self.answer("POST", f"{self.path}/payouts", body)


class Publishers(HubRoutes):
    """The publishers whose signed plugins the hub accepts (Gateway)."""

    path = "/v1/plugin-publishers"

    def list(self) -> Any:
        return self.hub.call("GET", self.path)

    def add(self, body: dict) -> Any:
        return self.hub.call("POST", self.path, body)

    def remove(self, signer: int) -> Any:
        return self.hub.call("DELETE", f"{self.path}/{signer}")
