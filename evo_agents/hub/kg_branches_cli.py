"""``evo-agents hub kg branch create|list|show|apply|diff|check|merge|drop``: the draft branches of a hub project's graph
(``docs/kg.md``, Draft branches). A branch is main with operations applied (node, edge, entity map and decision
changes) while main does not change; the kg tools read one with ``branch``. Writing needs the writer role. Standard
library only."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import call_object, client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_map, returns_object

BRANCH_KEYS = ("id", "name", "status", "base_build_id", "created_by", "created_at", "ops")


def _base(project: str, name: str | None = None) -> str:
    base = f"/v1/kg/{quote(project, safe='')}/branches"
    return base if name is None else f"{base}/{quote(name, safe='')}"


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


def _branch_line(branch: dict) -> str:
    return (
        f"branch {branch['name']}: {branch['status']}, {branch['ops']} operations, "
        f"from build {branch.get('base_build_id') or '-'}"
    )


@client_command
def cmd_branch_list(args) -> int:
    hub, _ = signed_in()
    path = _base(args.project) + (f"?status={args.status}" if args.status else "")
    found = call_object(hub, "GET", path)
    if args.json:
        print_json(found)
        return 0
    rows = [
        (b["name"], b["status"], str(b["ops"]), str(b.get("base_build_id") or "-"), b["created_at"])
        for b in found["branches"]
    ]
    table(("BRANCH", "STATUS", "OPS", "FROM BUILD", "CREATED"), rows)
    return 0


@client_command
def cmd_branch_create(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "POST", _base(args.project), {"name": args.name})
    if args.json:
        print_json(found)
        return 0
    print(f"created {_branch_line(found)}")
    return 0


@client_command
def cmd_branch_show(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "GET", _base(args.project, args.name))
    if args.json:
        print_json(found)
        return 0
    print(_branch_line(found))
    return 0


@client_command
def cmd_branch_apply(args) -> int:
    try:
        ops = read_ops(args.ops)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    hub, _ = signed_in()
    found = call_object(hub, "POST", f"{_base(args.project, args.name)}/ops", {"ops": ops})
    if args.json:
        print_json(found)
        return 0
    print(
        f"applied {found['applied']} operations to branch {args.name} ({found['ops']} in all): "
        f"{found['nodes']} nodes, {found['edges']} edges; main did not change"
    )
    return 0


def _counts(counts: dict) -> str:
    return ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in counts.items())


@client_command
def cmd_branch_diff(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "GET", f"{_base(args.project, args.name)}/diff")
    if args.json:
        print_json(found)
        return 0
    print(f"branch {args.name} against main: {_counts(found.get('counts') or {})}")
    for kind in ("nodes", "edges"):
        for change in ("added", "changed", "removed"):
            for item in (found.get(kind) or {}).get(change) or []:
                print(f"  {change} {kind[:-1]} {item.get('id')}")
    return 0


@client_command
def cmd_branch_check(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "GET", f"{_base(args.project, args.name)}/check")
    if args.json:
        print_json(found)
        return 0 if found.get("mergeable") else 1
    if not found.get("rules"):
        print("the project's ontology declares no rules")
    for v in found.get("new") or []:
        hard = " (hard: blocks the merge)" if v in (found.get("hard") or []) else ""
        print(f"  {v['rule']} {v['key']}{hard}: {v.get('detail') or ''}")
    for c in found.get("conflicts") or []:
        print(f"  conflict: operation {c['op']} read {c['id']}, which main changed since")
    state = "mergeable" if found.get("mergeable") else "not mergeable"
    print(
        f"branch {args.name}: {len(found.get('violations') or [])} violations, {len(found.get('new') or [])} new; {state}"
    )
    return 0 if found.get("mergeable") else 1


@client_command
def cmd_branch_merge(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "POST", f"{_base(args.project, args.name)}/merge")
    if args.json:
        print_json(found)
        return 0
    counts = {k: v for k, v in found.items() if k.startswith(("nodes_", "edges_"))}
    print(f"merged branch {args.name} into main ({found['ops']} operations): {_counts(counts)}")
    return 0


@client_command
def cmd_branch_drop(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "DELETE", _base(args.project, args.name))
    if args.json:
        print_json(found)
        return 0
    print(f"dropped branch {args.name} and its {found['ops']} operations; main did not change")
    return 0


def _named(sub, name: str, help: str, func, output) -> None:
    parser = sub.add_parser(name, help=help)
    parser.add_argument("name", metavar="NAME", help="the branch")
    parser.add_argument("--project", required=True, help="project name on the hub")
    if name == "apply":
        parser.add_argument(
            "--ops",
            required=True,
            metavar="FILE",
            help="JSON file of the operations, a list or {ops: [...]}; - reads stdin",
        )
    json_option(parser, output)
    parser.set_defaults(func=func)


def register(ksub) -> None:
    """``hub kg branch``, under the subparsers ``ksub`` of ``hub kg``."""
    branch = ksub.add_parser(
        "branch",
        help="draft branches of a project's graph: change nodes, edges and decisions apart from main, then merge",
        description="A draft branch is main with operations applied (node.put, node.delete, edge.put, edge.delete, "
        "entity_map, decision) while main does not change. Read it with the kg tools' branch argument, see its diff "
        "and the ontology rules it breaks, then merge it into main (refused when main changed what it changes, or it "
        "breaks a hard rule) or drop it.",
    )
    bsub = branch.add_subparsers(dest="branch_command", required=True)

    listed = bsub.add_parser("list", help="the draft branches of a project's graph, newest first")
    listed.add_argument("--project", required=True, help="project name on the hub")
    listed.add_argument("--status", choices=("open", "merged", "closed"), help="only branches in this state")
    json_option(listed, returns_object("branches", schema="BranchesOut"))
    listed.set_defaults(func=cmd_branch_list)

    create = bsub.add_parser("create", help="open a draft branch from main (writer role)")
    create.add_argument("name", metavar="NAME", help="a-z, 0-9, '.', '_', '-'; not 'main'")
    create.add_argument("--project", required=True, help="project name on the hub")
    json_option(create, returns_object(*BRANCH_KEYS, schema="BranchOut"))
    create.set_defaults(func=cmd_branch_create)

    _named(bsub, "show", "one draft branch", cmd_branch_show, returns_object(*BRANCH_KEYS, schema="BranchOut"))
    _named(bsub, "apply", "apply operations to an open branch, all or none", cmd_branch_apply, returns_map())
    _named(bsub, "diff", "the nodes and edges a branch adds, changes and removes", cmd_branch_diff, returns_map())
    _named(
        bsub,
        "check",
        "the ontology rules a branch breaks and whether it merges (exit 1: not)",
        cmd_branch_check,
        returns_map(),
    )
    _named(bsub, "merge", "merge an open branch into main", cmd_branch_merge, returns_map())
    _named(bsub, "drop", "drop an open branch and its operations", cmd_branch_drop, returns_map())
