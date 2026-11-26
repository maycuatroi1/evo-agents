"""``evo-agents hub kg branch create|list|show|apply|diff|check|merge|drop``: the draft branches of a hub project's graph
(``docs/kg.md``, Draft branches). A branch is main with operations applied (node, edge, entity map and decision
changes) while main does not change; the kg tools read one with ``branch``. Writing needs the writer role. Standard
library only.

:data:`BRANCH` is the group of class commands of ``evo_agents.clikit``; :class:`KgBranches` is the gateway to its
routes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_map, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

BRANCH_KEYS = ("id", "name", "status", "base_build_id", "created_by", "created_at", "ops")
BRANCH_OUTPUT = returns_object(*BRANCH_KEYS, schema="BranchOut")


class KgBranches(KgResource):
    """The draft branches of a project's graph: /v1/kg/{p}/branches, .../branches/{name} and what is under it."""

    collection = "branches"

    def list(self, status: str | None = None) -> dict:
        return self.object("GET", status=status)

    def create(self, name: str) -> dict:
        return self.object("POST", body={"name": name})

    def ask(self, method: str, name: str, *parts: str, body: dict | None = None) -> dict:
        """``method`` on the branch ``name``, or on ``parts`` under it."""
        return self.object(method, name, *parts, body=body)


def read_ops(source: str) -> list:
    """The operations in JSON file ``source`` (``-`` for stdin): a list of them, or an object with ``ops``. Raises
    ValueError for anything else."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    found = json.loads(text)
    if isinstance(found, dict):
        found = found.get("ops")
    if not isinstance(found, list) or not found:
        raise ValueError(f"{source} holds no list of operations (a list, or an object with ops)")
    return found


def branch_line(branch: dict) -> str:
    return (
        f"branch {branch['name']}: {branch['status']}, {branch['ops']} operations, "
        f"from build {branch.get('base_build_id') or '-'}"
    )


def counts_text(counts: dict) -> str:
    return ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in counts.items())


def diff_lines(name: str, found: dict) -> list[str]:
    lines = [f"branch {name} against main: {counts_text(found.get('counts') or {})}"]
    for kind in ("nodes", "edges"):
        for change in ("added", "changed", "removed"):
            lines += [f"  {change} {kind[:-1]} {item.get('id')}" for item in (found.get(kind) or {}).get(change) or []]
    return lines


def check_lines(name: str, found: dict) -> list[str]:
    lines = [] if found.get("rules") else ["the project's ontology declares no rules"]
    for v in found.get("new") or []:
        hard = " (hard: blocks the merge)" if v in (found.get("hard") or []) else ""
        lines.append(f"  {v['rule']} {v['key']}{hard}: {v.get('detail') or ''}")
    for c in found.get("conflicts") or []:
        lines.append(f"  conflict: operation {c['op']} read {c['id']}, which main changed since")
    state = "mergeable" if found.get("mergeable") else "not mergeable"
    violations, new = len(found.get("violations") or []), len(found.get("new") or [])
    lines.append(f"branch {name}: {violations} violations, {new} new; {state}")
    return lines


NAMED = (Arg("name", metavar="NAME", help="the branch"), KG_PROJECT)
OPS = Arg(
    "--ops",
    required=True,
    metavar="FILE",
    help="JSON file of the operations, a list or {ops: [...]}; - reads stdin",
)


class BranchList(HubCommand):
    name = "list"
    help = "the draft branches of a project's graph, newest first"
    arguments = (
        KG_PROJECT,
        Arg("--status", choices=("open", "merged", "closed"), help="only branches in this state"),
    )
    output = returns_object("branches", schema="BranchesOut")

    def run(self, ctx: HubContext) -> Result:
        found = KgBranches.of(ctx).list(ctx.args.status or None)
        rows = [
            (b["name"], b["status"], str(b["ops"]), str(b.get("base_build_id") or "-"), b["created_at"])
            for b in found["branches"]
        ]
        return Result(found, Table(("BRANCH", "STATUS", "OPS", "FROM BUILD", "CREATED"), rows, bare=True))


class BranchCreate(HubCommand):
    name = "create"
    help = "open a draft branch from main (writer role)"
    arguments = (Arg("name", metavar="NAME", help="a-z, 0-9, '.', '_', '-'; not 'main'"), KG_PROJECT)
    output = BRANCH_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = KgBranches.of(ctx).create(ctx.args.name)
        return Result(found, Text(f"created {branch_line(found)}"))


class BranchShow(HubCommand):
    name = "show"
    help = "one draft branch"
    arguments = NAMED
    output = BRANCH_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = KgBranches.of(ctx).ask("GET", ctx.args.name)
        return Result(found, Text(branch_line(found)))


class BranchApply(HubCommand):
    name = "apply"
    help = "apply operations to an open branch, all or none"
    arguments = (*NAMED, OPS)
    output = returns_map()

    def run(self, ctx: HubContext) -> Result:
        name = ctx.args.name
        try:
            ops = read_ops(ctx.args.ops)  # before signing in, as always
        except (OSError, ValueError) as exc:
            raise UsageError(str(exc)) from None
        found = KgBranches.of(ctx).ask("POST", name, "ops", body={"ops": ops})
        line = (
            f"applied {found['applied']} operations to branch {name} ({found['ops']} in all): "
            f"{found['nodes']} nodes, {found['edges']} edges; main did not change"
        )
        return Result(found, Text(line))


class BranchDiff(HubCommand):
    name = "diff"
    help = "the nodes and edges a branch adds, changes and removes"
    arguments = NAMED
    output = returns_map()

    def run(self, ctx: HubContext) -> Result:
        found = KgBranches.of(ctx).ask("GET", ctx.args.name, "diff")
        return Result(found, Text(*diff_lines(ctx.args.name, found)))


class BranchCheck(HubCommand):
    name = "check"
    help = "the ontology rules a branch breaks and whether it merges (exit 1: not)"
    arguments = NAMED
    output = returns_map()

    def run(self, ctx: HubContext) -> Result:
        found = KgBranches.of(ctx).ask("GET", ctx.args.name, "check")
        return Result(found, Text(*check_lines(ctx.args.name, found)), 0 if found.get("mergeable") else 1)


class BranchMerge(HubCommand):
    name = "merge"
    help = "merge an open branch into main"
    arguments = NAMED
    output = returns_map()

    def run(self, ctx: HubContext) -> Result:
        name = ctx.args.name
        found = KgBranches.of(ctx).ask("POST", name, "merge")
        counts = {k: v for k, v in found.items() if k.startswith(("nodes_", "edges_"))}
        return Result(found, Text(f"merged branch {name} into main ({found['ops']} operations): {counts_text(counts)}"))


class BranchDrop(HubCommand):
    name = "drop"
    help = "drop an open branch and its operations"
    arguments = NAMED
    output = returns_map()

    def run(self, ctx: HubContext) -> Result:
        name = ctx.args.name
        found = KgBranches.of(ctx).ask("DELETE", name)
        return Result(found, Text(f"dropped branch {name} and its {found['ops']} operations; main did not change"))


BRANCH = Group(
    "branch",
    "draft branches of a project's graph: change nodes, edges and decisions apart from main, then merge",
    (
        BranchList(),
        BranchCreate(),
        BranchShow(),
        BranchApply(),
        BranchDiff(),
        BranchCheck(),
        BranchMerge(),
        BranchDrop(),
    ),
    description="A draft branch is main with operations applied (node.put, node.delete, edge.put, edge.delete, "
    "entity_map, decision) while main does not change. Read it with the kg tools' branch argument, see its diff "
    "and the ontology rules it breaks, then merge it into main (refused when main changed what it changes, or it "
    "breaks a hard rule) or drop it.",
)
NODES = (BRANCH,)
