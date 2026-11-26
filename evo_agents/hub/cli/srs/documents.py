"""``hub srs list|show|import|export|retire|archive|unarchive``: the BA documents of a project, their items, and
their Markdown both ways."""

from __future__ import annotations

import sys
from pathlib import Path

from evo_agents.clikit import (
    Arg,
    Result,
    Stderr,
    Table,
    Text,
    UsageError,
    Verbatim,
    View,
    returns_array,
    returns_object,
)
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.features import answer
from evo_agents.hub.cli.srs.keys import (
    DOCUMENT_KEYS,
    IMPORT_KEYS,
    ITEM_DETAIL_KEYS,
    KINDS,
    PROJECT,
    RETIRE_KEYS,
    SLUG,
    SUMMARY_KEYS,
)
from evo_agents.hub.cli.srs.resources import Documents
from evo_agents.hub.cli.srs.views import document_view, item_view, items, preview_view
from evo_agents.hub.client import HubError

VERSION = Arg("--version", type=int, metavar="N", help="this version instead of the latest")


def list_table(found: list[dict], everything: bool, project: str) -> View:
    if not found:
        return Text(f"Project {project} has no BA document: `evo-agents hub srs import FILE --project {project}`.")
    rows = [
        (d["slug"], d["kind"], d["latest_version"] or "-", d["latest_state"] or "-", d["approved_version"] or "-")
        for d in found
    ]
    if everything:
        rows = [(*row, when(d["archived_at"])) for row, d in zip(rows, found, strict=True)]
    return Table(("SLUG", "KIND", "LATEST", "STATE", "APPROVED", *(("ARCHIVED",) if everything else ())), rows)


class SrsList(HubCommand):
    name = "list"
    help = "the documents, with their latest and approved versions"
    arguments = (PROJECT, Arg("--all", action="store_true", help="the archived documents too"))
    output = returns_array(*SUMMARY_KEYS, schema="SrsDocumentSummary")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        return answer(ctx, Documents.of(ctx).list(args.all), lambda found: list_table(found, args.all, args.project))


class SrsShow(HubCommand):
    name = "show"
    help = "a document's versions and items, or one item"
    arguments = (SLUG, PROJECT, VERSION, Arg("--item", metavar="CODE", help="one item, by code, alias or key"))
    output = returns_object(
        *DOCUMENT_KEYS,
        schema="SrsDocument",
        variants=(("--item", returns_object(*ITEM_DETAIL_KEYS, schema="SrsItemDetail")),),
    )

    def run(self, ctx: HubContext) -> Result:
        documents, args = Documents.of(ctx), ctx.args
        if args.item:
            return answer(ctx, documents.item(args.slug, args.item, args.version), item_view)
        return answer(ctx, documents.get(args.slug, args.version), document_view)


def read_markdown(file: str) -> str:
    if file == "-":
        return sys.stdin.read()
    try:
        return Path(file).expanduser().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None


class SrsImport(HubCommand):
    name = "import"
    help = "Markdown in the template's headings, as a document or its next version"
    arguments = (
        Arg("file", metavar="FILE", help="the Markdown file; - for stdin"),
        PROJECT,
        Arg("--doc", metavar="SLUG", help="import as the next version of this document"),
        Arg("--slug", metavar="SLUG", help="a new document's slug (default: from its title)"),
        Arg("--title", metavar="TITLE", help="a new document's title (default: its first bold line)"),
        Arg("--kind", choices=KINDS, default="srs", help="a new document's kind (default: srs)"),
        Arg("--summary", metavar="TEXT", help="what the version changes"),
        Arg("--dry-run", action="store_true", help="print the preview, import nothing"),
    )
    output = returns_object(*IMPORT_KEYS, schema="SrsImportResult")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"markdown": read_markdown(args.file), "kind": args.kind, "dry_run": args.dry_run}
        for name in ("doc", "slug", "title", "summary"):
            value = getattr(args, name)
            if value:
                body["document" if name == "doc" else name] = value
        if args.doc and (args.slug or args.title):  # after the file is read, as the command always did
            raise UsageError("--slug and --title name a new document; --doc imports the next version of one")
        return answer(ctx, Documents.of(ctx).import_(body), preview_view)


class SrsExport(HubCommand):
    name = "export"
    help = "a version as Markdown, with the same structure and codes"
    arguments = (
        SLUG,
        PROJECT,
        VERSION,
        Arg("--no-anchors", action="store_true", help="leave out the comments holding screens' codes"),
        Arg("--output", "-o", metavar="FILE", help="write this file instead of stdout"),
    )

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).export(args.slug, args.version, not args.no_anchors)
        if not args.output:
            return Result(found, Verbatim(found["markdown"]))
        Path(args.output).expanduser().write_text(found["markdown"], encoding="utf-8")
        return Result(found, Stderr(Text(f"Wrote version {found['version']} of {args.slug} to {args.output}.")))


def retire_view(slug: str, project: str, done: dict) -> Text:
    gone = done["dropped"]
    if done["dry_run"]:
        lines = [f"Retiring would drop {items(len(gone))} of {slug}; nothing is written (a dry run):"]
    else:
        lines = [f"Saved {slug} version {done['version']}, a draft without {items(len(gone))}:"]
    for one in gone:
        name = f"{one['code']}: {one['title']}" if one["code"] else one["title"]
        lines.append(f"  - {name} ({one['kind'].replace('_', ' ')})")
    if not done["dry_run"]:
        lines.append(f"`evo-agents hub srs submit {slug} {done['version']} --project {project}` sends it to review.")
    return Text(*lines)


class Retire(HubCommand):
    name = "retire"
    help = "drop coded items by code, subtrees too, as the next version (an admin)"
    arguments = (
        SLUG,
        Arg("codes", metavar="CODE", nargs="+", help='a code or an alias of an item, such as "UC ORD-1"'),
        PROJECT,
        Arg("--summary", metavar="TEXT", help="what the version changes (default: Retired and the codes)"),
        Arg("--dry-run", action="store_true", help="print the items that would be dropped, write nothing"),
    )
    output = returns_object(*RETIRE_KEYS, schema="SrsRetireResult")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"codes": args.codes, "dry_run": args.dry_run}
        if args.summary:
            body["summary"] = args.summary
        done = Documents.of(ctx).retire(args.slug, body)
        return answer(ctx, done, lambda found: retire_view(args.slug, args.project, found))


ARCHIVE_OUTPUT = returns_object(*SUMMARY_KEYS, schema="SrsDocumentSummary")


def archive_view(slug: str, project: str, found: dict) -> Text:
    if found["archived_at"]:
        return Text(
            f"Archived {slug}: it is out of `srs list`, search and the knowledge graph, still read by its slug, "
            f"and takes no change; `evo-agents hub srs unarchive {slug} --project {project}` opens it again."
        )
    return Text(f"Unarchived {slug}: it is in `srs list` again and takes changes.")


class Archive(HubCommand):
    name = "archive"
    help = "archive a document: out of list, search and the graph, closed to changes (an admin)"
    arguments = (SLUG, PROJECT)
    output = ARCHIVE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).archive(args.slug, "archive")
        return answer(ctx, found, lambda done: archive_view(args.slug, args.project, done))


class Unarchive(HubCommand):
    name = "unarchive"
    help = "open an archived document again (an admin)"
    arguments = (SLUG, PROJECT)
    output = ARCHIVE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Documents.of(ctx).archive(args.slug, "unarchive")
        return answer(ctx, found, lambda done: archive_view(args.slug, args.project, done))
