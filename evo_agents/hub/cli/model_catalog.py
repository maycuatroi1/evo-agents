"""``evo-agents hub model-catalog``: the providers a member can add an account for (GET /v1/model-catalog).

Prints one row per provider: id, name, group, API format, base URL and default model, then the OmniRoute version and
commit the catalog came from. ``--group`` keeps one group, ``--json`` prints what the hub answered. Standard library
only, like the rest of the client.

The command is a class of ``evo_agents.clikit`` (docs/structure.md, section Command classes); :class:`ModelCatalog`
is the gateway to the route, which belongs to the hub and not to a project.
"""

from __future__ import annotations

from typing import Any, ClassVar
from urllib.parse import quote

from evo_agents.clikit import Arg, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, HubRoutes

CATALOG_KEYS = ("source", "groups", "providers")


class ModelCatalog(HubRoutes):
    """GET /v1/model-catalog: the providers of the hub's model catalog (Gateway)."""

    base = "model-catalog"

    def get(self, group: str | None) -> Any:
        return self.hub.call("GET", self.path + (f"?group={quote(group)}" if group else ""))


def source_line(catalog: dict, count: int) -> str:
    source = catalog["source"]
    return (
        f"{count} providers, from {source['name']} {source['version']} ({source['commit'][:8]}, {source['license']})."
    )


class ModelCatalogCommand(HubCommand):
    name = "model-catalog"
    help = "the providers you can add an account for: id, group, API format, base URL, default model"
    arguments = (Arg("--group", metavar="GROUP", help="only this group, such as aggregator, regional or local"),)
    output = returns_object(*CATALOG_KEYS, schema="ModelCatalog")
    columns: ClassVar[tuple[str, ...]] = ("ID", "NAME", "GROUP", "FORMAT", "BASE URL", "DEFAULT MODEL")

    def run(self, ctx: HubContext) -> Result:
        found = ModelCatalog(ctx.hub).get(ctx.args.group)
        if ctx.args.json:
            return Result(found)
        rows = [
            (p["id"], p["name"], p["group"], p["api_format"], p["base_url"] or "-", p["default_model"] or "-")
            for p in found["providers"]
        ]
        return Result(found, Views(Table(self.columns, rows, bare=True), Text(source_line(found, len(rows)))))


MODEL_CATALOG = ModelCatalogCommand()
