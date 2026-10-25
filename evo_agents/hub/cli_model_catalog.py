"""``evo-agents hub model-catalog``: the providers a member can add an account for (GET /v1/model-catalog).

Prints one row per provider: id, name, group, API format, base URL and default model, then the OmniRoute version and
commit the catalog came from. ``--group`` keeps one group, ``--json`` prints what the hub answered. Standard library
only, like the rest of the client.
"""

from __future__ import annotations

from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object

CATALOG_KEYS = ("source", "groups", "providers")


@client_command
def cmd_model_catalog(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/model-catalog" + (f"?group={quote(args.group)}" if args.group else ""))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (p["id"], p["name"], p["group"], p["api_format"], p["base_url"] or "-", p["default_model"] or "-")
        for p in found["providers"]
    ]
    table(("ID", "NAME", "GROUP", "FORMAT", "BASE URL", "DEFAULT MODEL"), rows)
    source = found["source"]
    print(
        f"{len(rows)} providers, from {source['name']} {source['version']} ({source['commit'][:8]}, {source['license']})."
    )
    return 0


def register_model_catalog(hsub) -> None:
    parser = hsub.add_parser(
        "model-catalog", help="the providers you can add an account for: id, group, API format, base URL, default model"
    )
    parser.add_argument("--group", metavar="GROUP", help="only this group, such as aggregator, regional or local")
    json_option(parser, returns_object(*CATALOG_KEYS, schema="ModelCatalog"))
    parser.set_defaults(func=cmd_model_catalog)
