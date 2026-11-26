"""``evo-agents hub policy show|set|history|diff``: the autonomy policy of a project (``evo_agents.hub.autonomy``),
over GET and PUT /v1/projects/{p}/policy and the routes below it.

``show`` prints every key of the policy now, or of revision ``--revision``, each marked ``(default)`` when the
revision does not set it. ``set --file FILE`` writes the policy whole as its next revision, from a YAML or JSON file
(``-`` for stdin) of the sections ``repos``, ``merge``, ``judge``, ``breaker``, ``audit``, ``loop``, ``context`` and
``routing``, and prints the keys whose value changed; a file equal to the newest revision writes nothing. Only ``true``
and ``false`` are booleans in the file, so ``mode: off`` stays the word YAML 1.1 would read as false. ``history`` lists the revisions, newest first, with
the keys each changed, and ``diff FROM [TO]`` the keys whose value differs between two revisions (0 is no policy).

Every member of the project reads the policy; writing it needs the admin role on the project. Every command finds
its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the current directory.
``--json`` prints what the hub answered.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Policies` is
the gateway to the routes. Standard library and PyYAML only, like the rest of the client.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import ClassVar

import yaml

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.contract import returns_array, returns_object

MAX_FILE_BYTES = 64 * 1024
SHOWN_KEYS = 3  # changed keys a row of history names before "+N more"
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


class Policies(ProjectResource):
    """The autonomy policy of a project: GET and PUT /v1/projects/{p}/policy and the routes below it."""

    collection = "policy"

    def get(self, revision: int | None = None) -> dict:
        return self.call("GET") if revision is None else self.call("GET", "revisions", revision)

    def revisions(self) -> list[dict]:
        return self.call("GET", "revisions")

    def compare(self, base: int, head: int | None = None) -> dict:
        return self.call("GET", "compare", **{"from": base, "to": head})

    def put(self, body: dict) -> dict:
        return self.call("PUT", body=body)


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


NO_POLICY = "Project {project} has no policy: every repo is off and every key at its default."


def show_lines(found: dict) -> list[str]:
    """A revision of the policy as the terminal prints it: every key, each marked when the revision leaves it."""
    if found["revision"] == 0:
        lines = [NO_POLICY.format(project=found["project"])]
    else:
        lines = [
            f"Policy of project {found['project']}: revision {found['revision']}, written by {found['created_by']} "
            f"at {when(found['created_at'])} UTC"
        ]
        if found.get("note"):
            lines.append(f"  note: {found['note']}")
    held = flat(found["policy"])
    lines.extend(
        f"{key}: {_value(value)}{'' if key in held else ' (default)'}"
        for key, value in flat(found["effective"]).items()
    )
    return lines


def change_lines(changes: list[dict]) -> list[str]:
    return [f"  {change['key']}: {_value(change['before'])} -> {_value(change['after'])}" for change in changes]


class PolicyShow(HubCommand):
    name = "show"
    help = "every key of the policy, each marked (default) when the revision leaves it"
    arguments = (Arg("--revision", type=int, metavar="N", help="an older revision (default: the newest)"), PROJECT)
    output = returns_object(*POLICY_KEYS, schema="AutonomyPolicy")

    def run(self, ctx: HubContext) -> Result:
        found = Policies.of(ctx).get(ctx.args.revision)
        return Result(found, Text(*show_lines(found)))


class PolicyHistory(HubCommand):
    name = "history"
    help = "the revisions of the policy, newest first, with the keys each changed"
    arguments = (PROJECT,)
    output = returns_array(*REVISION_KEYS, schema="PolicyRevision")
    columns: ClassVar[tuple[str, ...]] = ("REVISION", "WRITTEN (UTC)", "BY", "CHANGED", "NOTE")

    def run(self, ctx: HubContext) -> Result:
        found = Policies.of(ctx).revisions()
        return Result(
            found, Table(self.columns, [self.row(item) for item in found], NO_POLICY.format(project=ctx.project))
        )

    @staticmethod
    def row(item: dict) -> tuple:
        changed = item["changed"][:SHOWN_KEYS]
        more = len(item["changed"]) - len(changed)
        return (
            item["revision"],
            when(item["created_at"]),
            item["created_by"],
            ", ".join(changed) + (f" +{more} more" if more else "") or "-",
            item["note"] or "",
        )


class PolicyDiff(HubCommand):
    name = "diff"
    help = "the keys whose value differs between two revisions of the policy"
    arguments = (
        Arg("base", metavar="FROM", type=int, help="the older revision; 0 for no policy"),
        Arg("head", metavar="TO", type=int, nargs="?", help="the newer one (default: the newest)"),
        PROJECT,
    )
    output = returns_object(*COMPARISON_KEYS, schema="PolicyComparison")

    def run(self, ctx: HubContext) -> Result:
        found = Policies.of(ctx).compare(ctx.args.base, ctx.args.head)
        base, head, project = found["from_revision"], found["to_revision"], ctx.project
        if not found["changes"]:
            return Result(found, Text(f"Revisions {base} and {head} of the policy of project {project} read the same."))
        said = f"From revision {base} to revision {head} of the policy of project {project}:"
        return Result(found, Text(said, *change_lines(found["changes"])))


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
        raise ValueError(
            f"{name} holds no object of sections (repos, merge, judge, breaker, audit, loop, context, routing)"
        )
    try:
        json.dumps(body)
    except (TypeError, ValueError):
        raise ValueError(f"{name} holds a value JSON does not write") from None
    return body


def policy_body(args) -> dict:
    """The body of PUT /v1/projects/{p}/policy for the command line ``args``; UsageError when the file does not
    read."""
    try:
        body = {"policy": read_policy(args.file)}
    except (ValueError, OSError) as exc:
        raise UsageError(str(exc)) from None
    if args.note:
        body["note"] = args.note
    if args.if_revision is not None:
        body["if_revision"] = args.if_revision
    return body


class PolicySet(HubCommand):
    name = "set"
    help = "write the policy whole as its next revision (the admin role on the project)"
    arguments = (
        Arg(
            "--file",
            required=True,
            metavar="PATH",
            help="the policy as YAML or JSON, - for stdin (sections: repos, "
            "merge, judge, breaker, audit, loop, context, routing; a key left out reads as its default)",
        ),
        Arg("--note", help="why, for the history (at most 1000 characters)"),
        Arg("--if-revision", type=int, metavar="N", help="refuse unless the policy is at revision N"),
        PROJECT,
    )
    output = returns_object(*POLICY_KEYS, "changed", schema="PolicyWritten")

    def run(self, ctx: HubContext) -> Result:
        body = policy_body(ctx.args)  # the file is read before the hub is asked
        policies = Policies.of(ctx)
        written = policies.put(body)
        revision, project = written["revision"], ctx.project
        if ctx.args.json:
            return Result(written)
        if not written["changed"]:
            return Result(
                written,
                Text(f"The policy of project {project} is this already, at revision {revision}: nothing written."),
            )
        lines = [f"Wrote revision {revision} of the policy of project {project}."]
        compared = policies.compare(revision - 1, revision)
        if compared["changes"]:
            lines += ["Changed:", *change_lines(compared["changes"])]
        return Result(written, Text(*lines))


POLICY = Group(
    "policy",
    "the autonomy policy of a project: what the hub merges and releases itself",
    [PolicyShow(), PolicySet(), PolicyHistory(), PolicyDiff()],
)
