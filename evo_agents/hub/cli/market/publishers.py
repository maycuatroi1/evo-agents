"""``evo-agents hub plugin publisher list|add|remove`` (hub admins): the publishers whose signed plugins the hub
accepts, and the signers it trusts for each. The group hangs under ``hub plugin`` (``evo_agents.hub.cli.plugin``
attaches :data:`PUBLISHER`). Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, returns_array
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.market.resources import PUBLISHER_KEYS, Publishers

PUBLISHERS_HEADER = ("ID", "PUBLISHER", "PLUGINS", "SIGNER", "TRUSTED BY")
PUBLISHERS_OUTPUT = returns_array(*PUBLISHER_KEYS, schema="PluginPublisher")


def publisher_rows(item: dict) -> list[tuple]:
    """One publisher's signers: the operator's first, then those hub admins trusted."""
    kind = "core" if item["core"] else "isolated"
    rows = []
    if item["operator"]:
        trusted = {signer["identity"] for signer in item["trusted"]}
        rows += [
            ("-", item["id"], kind, identity, "the operator")
            for identity in item["identities"]
            if identity not in trusted
        ]
    rows += [
        (signer["id"], item["id"], kind, signer["identity"], f"{signer['added_by']} at {when(signer['added_at'])}")
        for signer in item["trusted"]
    ]
    return rows


class PublisherList(HubCommand):
    name = "list"
    help = "every publisher and what signs for it: the operator's and the trusted"
    output = PUBLISHERS_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Publishers(ctx.hub).list()
        rows = [row for item in found for row in publisher_rows(item)]
        return Result(found, Table(PUBLISHERS_HEADER, rows, bare=True))


class PublisherAdd(HubCommand):
    name = "add"
    help = "trust a signer for a publisher"
    arguments = (
        Arg("publisher", metavar="PUBLISHER", help="the publisher's id, as its manifests name it"),
        Arg("signer", metavar="SIGNER", help="sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY"),
        Arg("--note", help="why, for the other hub admins"),
    )
    output = PUBLISHERS_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Publishers(ctx.hub).add({"publisher": args.publisher, "signer": args.signer, "note": args.note})
        return Result(found, Text(f"The hub trusts {args.signer} to sign the plugins of {args.publisher}."))


class PublisherRemove(HubCommand):
    name = "remove"
    help = "stop trusting a signer hub admins trusted"
    arguments = (Arg("id", type=int, metavar="ID", help="the signer's id, as `publisher list` prints it"),)
    output = PUBLISHERS_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Publishers(ctx.hub).remove(ctx.args.id)
        return Result(found, Text(f"The hub no longer trusts signer {ctx.args.id}; what it signed stays installed."))


PUBLISHER = Group(
    "publisher",
    "the publishers whose signed plugins the hub accepts (hub admins)",
    (PublisherList(), PublisherAdd(), PublisherRemove()),
)
