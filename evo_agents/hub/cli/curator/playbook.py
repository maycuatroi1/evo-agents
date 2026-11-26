"""``evo-agents hub curator playbook`` and ``evo-agents hub curator earlier``: what the Curator's review runs of a
project remember of it.

``curator playbook`` lists the entries of the project's playbook (GET /v1/projects/{p}/curator/playbook,
``evo_agents.hub.playbook``), the most helpful first, by ``--lens``, the retired ones too with ``--retired``; only the
agent of a review run changes it, by deltas (``evo-agents worker playbook``). ``curator earlier`` lists what earlier
review nights found, newest first, with what became of each finding (open, proposed, rejected, merged, kept or
reverted) and its proposals, and the proposals the owner rejected lately (GET .../curator/earlier).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Memory` is the
gateway to the routes.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.review import LENSES

PLAYBOOK_KEYS = ("project", "entries", "live", "retired")
EARLIER_KEYS = ("project", "findings", "rejected")
STATUSES = ("open", "proposed", "rejected", "merged", "kept", "reverted")  # server.earlier.STATUSES


class Memory(ProjectResource):
    """What the review runs remember: GET /v1/projects/{p}/curator/playbook and .../curator/earlier."""

    collection = "curator"

    def playbook(self, lens: str | None, retired: bool) -> dict:
        return self.call("GET", "playbook", lens=lens, retired="true" if retired else None)

    def earlier(self, lens: str | None, status: str | None, limit: int) -> dict:
        return self.call("GET", "earlier", lens=lens, status=status, limit=limit)


class Playbook(HubCommand):
    name = "playbook"
    help = "the Curator's playbook of the project, the most helpful entries first"
    arguments = (
        PROJECT,
        Arg("--lens", choices=list(LENSES), help="its entries and those of every lens"),
        Arg("--retired", action="store_true", help="the retired entries too"),
    )
    output = returns_object(*PLAYBOOK_KEYS, schema="PlaybookView")
    columns = ("ENTRY", "LENS", "HELPFUL", "HARMFUL", "SHOWN", "UNTIL", "STATE", "TEXT")

    def run(self, ctx: HubContext) -> Result:
        found = Memory.of(ctx).playbook(ctx.args.lens, ctx.args.retired)
        rows = [self.row(entry) for entry in found["entries"]]
        said = f"The playbook of {ctx.project}: {found['live']} live entries, {found['retired']} retired"
        return Result(found, Views(Table(self.columns, rows), Text(said)))

    @staticmethod
    def row(entry: dict) -> tuple:
        return (
            entry["ref"],
            entry["lens"] or "every lens",
            str(entry["helpful"]),
            str(entry["harmful"]),
            str(entry.get("prompt_exposures") or 0),
            f"#{entry['until_proposal_id']}" if entry.get("until_proposal_id") else "-",
            "retired" if entry["retired"] else "live",
            entry["text"],
        )


class Earlier(HubCommand):
    name = "earlier"
    help = "what earlier review nights found and what became of it, newest first"
    arguments = (
        PROJECT,
        Arg("--lens", choices=list(LENSES)),
        Arg("--status", choices=STATUSES),
        Arg("--limit", type=int, default=20, choices=range(1, 101), metavar="N", help="1 to 100"),
    )
    output = returns_object(*EARLIER_KEYS, schema="EarlierView")
    columns = ("FINDING", "LENS", "NIGHT", "STATUS", "PROPOSALS", "TITLE")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Memory.of(ctx).earlier(args.lens, args.status, args.limit)
        rows = [self.row(item) for item in found["findings"]]
        lines = [f"{len(rows)} finding(s) of earlier nights in {ctx.project}"]
        for item in found["rejected"]:
            note = f" ({item['note']})" if item["note"] else ""
            lines.append(f"rejected lately: #{item['id']} {item['lens']} {item['kind']}: {item['title']}{note}")
        return Result(found, Views(Table(self.columns, rows), Text(*lines)))

    @staticmethod
    def row(item: dict) -> tuple:
        return (
            f"#{item['id']}",
            item["lens"],
            item["night"] or "-",
            item["status"] + (" (closed)" if item["closed"] else ""),
            ", ".join(f"#{p['id']} {p['status']}" for p in item["proposals"]) or "-",
            item["title"],
        )


PLAYBOOK = Playbook()
EARLIER = Earlier()
