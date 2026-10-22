"""``evo-agents hub policy show|set|history|diff``: the autonomy policy of a project (``evo_agents.hub.autonomy``),
over GET and PUT /v1/projects/{p}/policy and the routes below it.

``show`` prints every key of the policy now, or of revision ``--revision``, each marked ``(default)`` when the
revision does not set it. ``set --file FILE`` writes the policy whole as its next revision, from a YAML or JSON file
(``-`` for stdin) of the sections ``repos``, ``merge``, ``judge``, ``breaker`` and ``audit``, and prints the keys whose
value changed; a file equal to the newest revision writes nothing. Only ``true`` and ``false`` are booleans in the
file, so ``mode: off`` stays the word YAML 1.1 would read as false. ``history`` lists the revisions, newest first, with
the keys each changed, and ``diff FROM [TO]`` the keys whose value differs between two revisions (0 is no policy).

Every member of the project reads the policy; writing it needs the admin role on the project. Every command finds
its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory.
``--json`` prints what the hub answered. Standard library and PyYAML only, like the rest of the client.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote

import yaml

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.mirror import harness_project, harness_root

EXIT_USAGE = 2
MAX_FILE_BYTES = 64 * 1024
SHOWN_KEYS = 3  # changed keys a row of history names before "+N more"
PROJECT_HELP = "hub project (default: hub.project in the harness.yaml around the current directory)"
# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
POLICY_KEYS = ("project", "revision", "policy", "effective", "note", "created_by", "created_at")
REVISION_KEYS = ("revision", "note", "created_by", "created_at", "changed")
COMPARISON_KEYS = ("project", "from_revision", "to_revision", "changes")
BOOLEAN = "tag:yaml.org,2002:bool"
TIMESTAMP = "tag:yaml.org,2002:timestamp"


class PolicyLoader(yaml.SafeLoader):
    """``yaml.safe_load`` with only true and false as booleans, and dates left as text."""


PolicyLoader.yaml_implicit_resolvers = {
    first: [(tag, pattern) for tag, pattern in resolvers if tag not in (BOOLEAN, TIMESTAMP)]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
PolicyLoader.add_implicit_resolver(BOOLEAN, re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF"))


def _project(args) -> str:
    if args.project:
        return args.project
    try:
        root = harness_root(None)
    except HubError:
        raise HubError(
            "which project? pass --project, or run this inside a harness whose harness.yaml has hub.project"
        ) from None
    return harness_project(root)


def _path(project: str, rest: str = "") -> str:
    return f"/v1/projects/{quote(project, safe='')}/policy{rest}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _value(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def flat(tree: dict, prefix: str = "") -> dict:
    """The leaves of ``tree`` by dotted path, as ``autonomy.flat`` names them."""
    leaves = {}
    for name, value in tree.items():
        path = f"{prefix}.{name}" if prefix else name
        if isinstance(value, dict):
            leaves.update(flat(value, path))
        else:
            leaves[path] = value
    return leaves


# Reading


def _show(found: dict) -> None:
    if found["revision"] == 0:
        print(f"Project {found['project']} has no policy: every repo is off and every key at its default.")
    else:
        print(
            f"Policy of project {found['project']}: revision {found['revision']}, written by {found['created_by']} "
            f"at {when(found['created_at'])} UTC"
        )
        if found.get("note"):
            print(f"  note: {found['note']}")
    held = flat(found["policy"])
    for key, value in flat(found["effective"]).items():
        print(f"{key}: {_value(value)}{'' if key in held else ' (default)'}")


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    found = hub.call("GET", _path(project, "" if args.revision is None else f"/revisions/{args.revision}"))
    if args.json:
        print_json(found)
    else:
        _show(found)
    return 0


@client_command
def cmd_history(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    found = hub.call("GET", _path(project, "/revisions"))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no policy: every repo is off and every key at its default.")
        return 0
    rows = []
    for item in found:
        changed = item["changed"][:SHOWN_KEYS]
        more = len(item["changed"]) - len(changed)
        rows.append(
            (
                item["revision"],
                when(item["created_at"]),
                item["created_by"],
                ", ".join(changed) + (f" +{more} more" if more else "") or "-",
                item["note"] or "",
            )
        )
    table(("REVISION", "WRITTEN (UTC)", "BY", "CHANGED", "NOTE"), rows)
    return 0


def _print_changes(changes: list[dict]) -> None:
    for change in changes:
        print(f"  {change['key']}: {_value(change['before'])} -> {_value(change['after'])}")


@client_command
def cmd_diff(args) -> int:
    hub, _ = signed_in()
    project = _project(args)
    query = f"?from={args.base}" + ("" if args.head is None else f"&to={args.head}")
    found = hub.call("GET", _path(project, "/compare" + query))
    if args.json:
        print_json(found)
        return 0
    base, head = found["from_revision"], found["to_revision"]
    if not found["changes"]:
        print(f"Revisions {base} and {head} of the policy of project {project} read the same.")
        return 0
    print(f"From revision {base} to revision {head} of the policy of project {project}:")
    _print_changes(found["changes"])
    return 0


# Writing


def read_policy(name: str) -> dict:
    """The policy in file ``name`` (``-`` for stdin), YAML or JSON; ValueError saying why it does not read."""
    text = sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8")
    if len(text.encode()) > MAX_FILE_BYTES:
        raise ValueError(f"{name} is over {MAX_FILE_BYTES // 1024} KiB")
    try:
        body = yaml.load(text, Loader=PolicyLoader)  # noqa: S506 - a SafeLoader with fewer implicit types
    except yaml.YAMLError as exc:
        raise ValueError(f"{name} is not YAML: {exc}") from None
    body = {} if body is None else body
    if not isinstance(body, dict):
        raise ValueError(f"{name} holds no object of sections (repos, merge, judge, breaker, audit, loop)")
    try:
        json.dumps(body)
    except (TypeError, ValueError):
        raise ValueError(f"{name} holds a value JSON does not write") from None
    return body


@client_command
def cmd_set(args) -> int:
    try:
        body = {"policy": read_policy(args.file)}
    except (ValueError, OSError) as exc:
        return _usage(str(exc))
    if args.note:
        body["note"] = args.note
    if args.if_revision is not None:
        body["if_revision"] = args.if_revision
    hub, _ = signed_in()
    project = _project(args)
    written = hub.call("PUT", _path(project), body)
    if args.json:
        print_json(written)
        return 0
    revision = written["revision"]
    if not written["changed"]:
        print(f"The policy of project {project} is this already, at revision {revision}: nothing written.")
        return 0
    print(f"Wrote revision {revision} of the policy of project {project}.")
    compared = hub.call("GET", _path(project, f"/compare?from={revision - 1}&to={revision}"))
    if compared["changes"]:
        print("Changed:")
        _print_changes(compared["changes"])
    return 0


# Registering


def register_policy(hsub) -> None:
    policy = hsub.add_parser("policy", help="the autonomy policy of a project: what the hub merges and releases itself")
    psub = policy.add_subparsers(dest="policy_command", required=True)

    show = psub.add_parser("show", help="every key of the policy, each marked (default) when the revision leaves it")
    show.add_argument("--revision", type=int, metavar="N", help="an older revision (default: the newest)")
    show.add_argument("--project", help=PROJECT_HELP)
    json_option(show, returns_object(*POLICY_KEYS, schema="AutonomyPolicy"))
    show.set_defaults(func=cmd_show)

    written = psub.add_parser("set", help="write the policy whole as its next revision (the admin role on the project)")
    written.add_argument(
        "--file",
        required=True,
        metavar="PATH",
        help="the policy as YAML or JSON, - for stdin (sections: repos, "
        "merge, judge, breaker, audit, loop; a key left out reads as its default)",
    )
    written.add_argument("--note", help="why, for the history (at most 1000 characters)")
    written.add_argument("--if-revision", type=int, metavar="N", help="refuse unless the policy is at revision N")
    written.add_argument("--project", help=PROJECT_HELP)
    json_option(written, returns_object(*POLICY_KEYS, "changed", schema="PolicyWritten"))
    written.set_defaults(func=cmd_set)

    history = psub.add_parser("history", help="the revisions of the policy, newest first, with the keys each changed")
    history.add_argument("--project", help=PROJECT_HELP)
    json_option(history, returns_array(*REVISION_KEYS, schema="PolicyRevision"))
    history.set_defaults(func=cmd_history)

    diff = psub.add_parser("diff", help="the keys whose value differs between two revisions of the policy")
    diff.add_argument("base", metavar="FROM", type=int, help="the older revision; 0 for no policy")
    diff.add_argument("head", metavar="TO", type=int, nargs="?", help="the newer one (default: the newest)")
    diff.add_argument("--project", help=PROJECT_HELP)
    json_option(diff, returns_object(*COMPARISON_KEYS, schema="PolicyComparison"))
    diff.set_defaults(func=cmd_diff)
