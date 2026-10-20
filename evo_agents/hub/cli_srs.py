"""``evo-agents hub srs list|show|import|export|submit|approve|withdraw|archive|unarchive``: the BA documents of a
project that enabled the core plugin srs, through its routes under /v1/x/srs/<project>/ (``docs/plugins.md``, Core
plugins). The plugin's code is ``evo_agents.core_plugins.srs``, which this client never imports: it speaks HTTP only.

``list`` prints the documents with their latest and approved versions, ``--all`` the archived ones too. ``show SLUG``
prints a document's versions and
the items of its latest version (``--version N`` another), ``--item CODE`` one item with its fields and children.
``import FILE`` reads Markdown (``-`` for stdin) whose headings follow the template, as a new document (``--slug``,
``--title``) or as the next version of ``--doc``; ``--dry-run`` prints the preview only. ``export SLUG`` prints a
version as Markdown (``--no-anchors`` leaves out the screens' code comments, ``--output`` writes a file). ``submit
SLUG N`` sends a draft to review; ``approve SLUG N`` approves it, ``--request-changes`` sends it back with
``--comment``. ``withdraw SLUG N`` takes a version in review out of review for good, as its author or an admin, with
the reason as a comment on it (``--comment``). ``archive SLUG``, an admin's, takes a document out of the list, @ba's
search and the knowledge graph and closes it to changes; ``unarchive SLUG`` opens it again. ``--json`` prints what the
hub answered. A refusal prints the hub's reason and each problem it listed.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_FAILED, EXIT_USAGE = 1, 2
ROLES = ("ba", "qa", "dev")
KINDS = ("srs", "spec")
SUMMARY_KEYS = (
    "slug",
    "title",
    "kind",
    "template",
    "approved_version",
    "latest_version",
    "latest_state",
    "label",
    "created_by",
    "created_at",
    "archived_at",
)
DOCUMENT_KEYS = (*SUMMARY_KEYS, "versions", "shown")
ITEM_KEYS = ("key", "kind", "code", "aliases", "title", "parent", "heading_level", "fields", "body", "refs")
ITEM_DETAIL_KEYS = (*ITEM_KEYS, "document", "version", "path", "children")
IMPORT_KEYS = ("document", "created", "version", "dry_run", "preview")
VERSION_KEYS = (
    "number",
    "parent_number",
    "state",
    "round",
    "author",
    "drafted_by_run",
    "summary",
    "created_at",
    "submitted_at",
    "decided_at",
    "reviews",
)


def _path(project: str, *parts: str, query: dict | None = None) -> str:
    path = "/v1/x/srs/" + "/".join(quote(str(part), safe="") for part in (project, *parts))
    given = {key: value for key, value in (query or {}).items() if value is not None}
    return f"{path}?{urlencode(given)}" if given else path


def _problems(exc: HubError) -> None:
    """Each problem a refusal of the hub listed, one line each, on stderr."""
    detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
    for problem in detail or []:
        if isinstance(problem, dict) and "check" in problem:
            where = f" line {problem['line']}" if problem.get("line") else ""
            code = f" {problem['code']}" if problem.get("code") else ""
            print(f"  {problem['level']}: {problem['check']}{code}{where}: {problem['message']}", file=sys.stderr)
        elif isinstance(problem, dict) and "latest" in problem:
            print(f"  conflict on {problem.get('code') or problem.get('item') or 'the preamble'}", file=sys.stderr)


def _call(method: str, path: str, body: dict | None = None):
    hub, _ = signed_in()
    try:
        return hub.call(method, path, body)
    except HubError as exc:
        _problems(exc)
        raise


@client_command
def cmd_list(args) -> int:
    found = _call("GET", _path(args.project, "documents", query={"all": "true" if args.all else None}))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {args.project} has no BA document: `evo-agents hub srs import FILE --project {args.project}`.")
        return 0
    rows = [
        (d["slug"], d["kind"], d["latest_version"] or "-", d["latest_state"] or "-", d["approved_version"] or "-")
        for d in found
    ]
    if args.all:
        rows = [(*row, when(d["archived_at"])) for row, d in zip(rows, found, strict=True)]
    table(("SLUG", "KIND", "LATEST", "STATE", "APPROVED", *(("ARCHIVED",) if args.all else ())), rows)
    return 0


def _print_item(item: dict) -> None:
    print(f"{item['code'] or item['title']} ({item['kind'].replace('_', ' ')}, key {item['key']}): {item['title']}")
    if item["aliases"]:
        print(f"  also: {', '.join(item['aliases'])}")
    print(
        f"  in {item['document']} version {item['version']}"
        + (f", under {' > '.join(item['path'])}" if item["path"] else "")
    )
    for name, value in item["fields"].items():
        if isinstance(value, str) and name not in ("tail", "dash", "step_label"):
            print(f"  {name}: {value}")
    for step in item["fields"].get("steps") or []:
        print(f"  step {step}" if isinstance(step, str) else "  step " + " | ".join(str(v) for v in step.values()))
    for child in item["children"]:
        print(f"  - {child['code'] or child['title']} ({child['kind'].replace('_', ' ')}): {child['title']}")


def _print_document(doc: dict) -> None:
    approved = f", approved version {doc['approved_version']}" if doc["approved_version"] else ""
    print(f"{doc['title']} ({doc['slug']}, {doc['kind']}){approved}")
    for version in doc["versions"]:
        reviews = ", ".join(
            f"{r['decision']} by {r['reviewer']}" for r in version["reviews"] if r["round"] == version["round"]
        )
        line = f"  v{version['number']} {version['state']} by {version['author']} {when(version['created_at'])}"
        print(line + (f" ({reviews})" if reviews else "") + (f": {version['summary']}" if version["summary"] else ""))
    shown = doc["shown"]
    if shown is None:
        return
    print(f"Version {shown['number']}:")
    depth = {}
    for item in shown["items"]:
        depth[item["key"]] = depth.get(item["parent"], -1) + 1 if item["parent"] else 0
        name = f"{item['code']}: {item['title']}" if item["code"] else item["title"]
        print("  " + "  " * depth[item["key"]] + name)
    for problem in shown["problems"]:
        print(f"  {problem['level']}: {problem['check']} {problem['code'] or ''}: {problem['message']}")


@client_command
def cmd_show(args) -> int:
    if args.item:
        found = _call(
            "GET", _path(args.project, "documents", args.slug, "items", args.item, query={"version": args.version})
        )
    else:
        found = _call("GET", _path(args.project, "documents", args.slug, query={"version": args.version}))
    if args.json:
        print_json(found)
    elif args.item:
        _print_item(found)
    else:
        _print_document(found)
    return 0


def _read(file: str) -> str:
    if file == "-":
        return sys.stdin.read()
    try:
        return Path(file).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None


def _print_preview(done: dict) -> None:
    preview = done["preview"]
    counts = ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(preview["counts"].items()))
    if done["dry_run"]:
        print(f"Preview of {done['document']}: {counts}.")
    else:
        verb = "Created" if done["created"] else "Imported into"
        print(f"{verb} {done['document']}: version {done['version']}, a draft; {counts}.")
    for name in ("added", "changed", "removed"):
        if preview[name]:
            print(f"  {name}: {', '.join(preview[name])}")
    for problem in preview["problems"]:
        where = f" line {problem['line']}" if problem["line"] else ""
        print(f"  {problem['level']}: {problem['check']}{where}: {problem['message']}")


@client_command
def cmd_import(args) -> int:
    body = {"markdown": _read(args.file), "kind": args.kind, "dry_run": args.dry_run}
    for name in ("doc", "slug", "title", "summary"):
        value = getattr(args, name)
        if value:
            body["document" if name == "doc" else name] = value
    if args.doc and (args.slug or args.title):
        print("error: --slug and --title name a new document; --doc imports the next version of one", file=sys.stderr)
        return EXIT_USAGE
    done = _call("POST", _path(args.project, "import"), body)
    if args.json:
        print_json(done)
    else:
        _print_preview(done)
    return 0


@client_command
def cmd_export(args) -> int:
    query = {"version": args.version, "anchors": "false" if args.no_anchors else None}
    found = _call("GET", _path(args.project, "documents", args.slug, "export", query=query))
    if args.output:
        Path(args.output).expanduser().write_text(found["markdown"], encoding="utf-8")
        print(f"Wrote version {found['version']} of {args.slug} to {args.output}.", file=sys.stderr)
    else:
        sys.stdout.write(found["markdown"])
    return 0


def _print_version(slug: str, version: dict) -> None:
    print(f"{slug} version {version['number']} is {version['state'].replace('_', ' ')} (round {version['round']}).")
    for review in version["reviews"]:
        if review["round"] == version["round"]:
            role = f" as {review['role']}" if review["role"] else ""
            print(f"  {review['decision'].replace('_', ' ')} by {review['reviewer']}{role}")


@client_command
def cmd_submit(args) -> int:
    found = _call("POST", _path(args.project, "documents", args.slug, "versions", str(args.number), "submit"))
    if args.json:
        print_json(found)
    else:
        _print_version(args.slug, found)
    return 0


@client_command
def cmd_approve(args) -> int:
    if args.request_changes and not args.comment:
        print("error: --request-changes needs --comment, the changes the author is to make", file=sys.stderr)
        return EXIT_USAGE
    body = {"decision": "request_changes" if args.request_changes else "approve", "body": args.comment or ""}
    if args.role:
        body["role"] = args.role
    found = _call("POST", _path(args.project, "documents", args.slug, "versions", str(args.number), "reviews"), body)
    if args.json:
        print_json(found)
    else:
        _print_version(args.slug, found)
    return 0


@client_command
def cmd_withdraw(args) -> int:
    body = {"comment": args.comment} if args.comment else {}
    found = _call("POST", _path(args.project, "documents", args.slug, "versions", str(args.number), "withdraw"), body)
    if args.json:
        print_json(found)
    else:
        _print_version(args.slug, found)
    return 0


@client_command
def cmd_archive(args) -> int:
    found = _call("POST", _path(args.project, "documents", args.slug, args.srs_command))  # archive or unarchive
    if args.json:
        print_json(found)
    elif found["archived_at"]:
        print(
            f"Archived {args.slug}: it is out of `srs list`, search and the knowledge graph, still read by its slug, "
            f"and takes no change; `evo-agents hub srs unarchive {args.slug} --project {args.project}` opens it again."
        )
    else:
        print(f"Unarchived {args.slug}: it is in `srs list` again and takes changes.")
    return 0


def _project(parser) -> None:
    parser.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin srs")


def _register_reading(ssub) -> None:
    listed = ssub.add_parser("list", help="the documents, with their latest and approved versions")
    _project(listed)
    listed.add_argument("--all", action="store_true", help="the archived documents too")
    json_option(listed, returns_array(*SUMMARY_KEYS, schema="SrsDocumentSummary"))
    listed.set_defaults(func=cmd_list)

    show = ssub.add_parser("show", help="a document's versions and items, or one item")
    show.add_argument("slug", metavar="SLUG")
    _project(show)
    show.add_argument("--version", type=int, metavar="N", help="this version instead of the latest")
    show.add_argument("--item", metavar="CODE", help="one item, by code, alias or key")
    item_output = returns_object(*ITEM_DETAIL_KEYS, schema="SrsItemDetail")
    json_option(show, returns_object(*DOCUMENT_KEYS, schema="SrsDocument", variants=(("--item", item_output),)))
    show.set_defaults(func=cmd_show)


def _register_markdown(ssub) -> None:
    imported = ssub.add_parser("import", help="Markdown in the template's headings, as a document or its next version")
    imported.add_argument("file", metavar="FILE", help="the Markdown file; - for stdin")
    _project(imported)
    imported.add_argument("--doc", metavar="SLUG", help="import as the next version of this document")
    imported.add_argument("--slug", metavar="SLUG", help="a new document's slug (default: from its title)")
    imported.add_argument("--title", metavar="TITLE", help="a new document's title (default: its first bold line)")
    imported.add_argument("--kind", choices=KINDS, default="srs", help="a new document's kind (default: srs)")
    imported.add_argument("--summary", metavar="TEXT", help="what the version changes")
    imported.add_argument("--dry-run", action="store_true", help="print the preview, import nothing")
    json_option(imported, returns_object(*IMPORT_KEYS, schema="SrsImportResult"))
    imported.set_defaults(func=cmd_import)

    exported = ssub.add_parser("export", help="a version as Markdown, with the same structure and codes")
    exported.add_argument("slug", metavar="SLUG")
    _project(exported)
    exported.add_argument("--version", type=int, metavar="N", help="this version instead of the latest")
    exported.add_argument("--no-anchors", action="store_true", help="leave out the comments holding screens' codes")
    exported.add_argument("--output", "-o", metavar="FILE", help="write this file instead of stdout")
    exported.set_defaults(func=cmd_export)


def _version_command(ssub, name: str, what: str):
    """The parser of a command on version N of document SLUG of --project; ``what`` is its help."""
    parser = ssub.add_parser(name, help=what)
    parser.add_argument("slug", metavar="SLUG")
    parser.add_argument("number", type=int, metavar="N")
    _project(parser)
    return parser


def _register_review(ssub) -> None:
    submit = _version_command(ssub, "submit", "send a draft to review")
    json_option(submit, returns_object(*VERSION_KEYS, schema="SrsVersion"))
    submit.set_defaults(func=cmd_submit)

    approve = _version_command(ssub, "approve", "approve a version in review, or send it back with --request-changes")
    approve.add_argument("--request-changes", action="store_true", help="send it back to draft instead")
    approve.add_argument("--comment", metavar="TEXT", help="what the review says; needed with --request-changes")
    approve.add_argument("--role", choices=ROLES, help="the role the review counts for (one you hold)")
    json_option(approve, returns_object(*VERSION_KEYS, schema="SrsVersion"))
    approve.set_defaults(func=cmd_approve)

    withdraw = _version_command(ssub, "withdraw", "withdraw a version in review, for good (its author or an admin)")
    withdraw.add_argument("--comment", metavar="TEXT", help="why, kept as a comment on the version")
    json_option(withdraw, returns_object(*VERSION_KEYS, schema="SrsVersion"))
    withdraw.set_defaults(func=cmd_withdraw)


def _register_documents(ssub) -> None:
    for name, what in (
        ("archive", "archive a document: out of list, search and the graph, closed to changes (an admin)"),
        ("unarchive", "open an archived document again (an admin)"),
    ):
        parser = ssub.add_parser(name, help=what)
        parser.add_argument("slug", metavar="SLUG")
        _project(parser)
        json_option(parser, returns_object(*SUMMARY_KEYS, schema="SrsDocumentSummary"))
        parser.set_defaults(func=cmd_archive)


def register_srs(hsub) -> None:
    srs = hsub.add_parser("srs", help="BA documents of a project: versions, coded items, approval (plugin srs)")
    ssub = srs.add_subparsers(dest="srs_command", required=True)
    _register_reading(ssub)
    _register_markdown(ssub)
    _register_review(ssub)
    _register_documents(ssub)
