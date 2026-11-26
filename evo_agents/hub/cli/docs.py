"""``evo-agents hub docs list|show|history|put``: the documents of a project the hub's feature docs is on in, through its
routes under /v1/x/docs/<project>/ (``docs/hub.md``, section Docs). The feature's code is ``evo_agents.hub.server.docs``,
which this client never imports: it speaks HTTP only.

``list`` prints the documents with their latest version. ``show SLUG`` prints a version as Markdown, the latest unless
``--version N``, its title as the first heading; ``--out DIR`` writes it to ``DIR/<slug>.md`` instead, with every
image it shows under DIR by the name the Markdown gives it. An HTML version prints its page as it is, and ``--out``
writes ``DIR/<slug>.html``. ``history SLUG`` prints every version.

``put FILE`` saves a Markdown file as the next version of a document, created at version 1 when the project has none of
that slug (``--slug``, by default from the file's name). Every image the Markdown shows from a file beside it,
``![alt](shots/board.png "caption")``, is uploaded first, once each (the hub keeps an image once per project), and the
version names it by its path from the file; a path the hub does not take as a name (``../board.png``, spaces) is
renamed under ``images/`` and the Markdown with it. An image of the web (``https://...``) stays as it is, and the hub's
web does not load it. A local image that is not there stops the command before anything is sent. The title is
``--title``, else the file's first line when it is a heading ``# Title``, which then leaves the body, else the slug.
A file ending in ``.html`` or ``.htm`` is saved as it is as an HTML page (format ``html``), which shows no image of
the project but embeds its own; its title is ``--title``, else the text of its ``<title>``, else the slug.
``--base N`` refuses the save unless the document is at version N; ``--label LEVEL`` gives a new document a label, an
admin's choice. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands): :class:`Documents` is the
gateway to the feature's routes, :class:`Prepared` the Markdown file made ready to save (:func:`prepare`).
"""

from __future__ import annotations

import argparse
import html
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, Verbatim, returns_array, returns_object
from evo_agents.hub.api import FeatureResource
from evo_agents.hub.cli.base import HubCommand, HubContext, when

SUMMARY_KEYS = (
    "slug",
    "title",
    "latest_version",
    "label",
    "created_by",
    "created_at",
    "updated_by",
    "updated_at",
    "format",
)
DOCUMENT_KEYS = (*SUMMARY_KEYS, "versions", "shown")
VERSION_KEYS = ("number", "title", "summary", "author", "created_at", "format")
SAVED_KEYS = ("document", "version", "created", "changed")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
IMAGE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,4}$")
IMAGE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\((?P<src><[^>\n]*>|[^\s)]+)"
    r"""(?P<title>\s+(?:"[^"\n]*"|'[^'\n]*'|\([^)\n]*\)))?\s*\)"""
)
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
TITLE = re.compile(r"^#\s+(?P<title>.+?)\s*#*\s*$")
PAGE_TITLE = re.compile(r"<title[^>]*>(?P<title>.*?)</title\s*>", re.IGNORECASE | re.DOTALL)
PAGE_SUFFIXES = (".html", ".htm")
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

PROJECT = Arg("--project", required=True, metavar="PROJECT", help="the project on the hub")
SLUG_ARG = Arg("slug", metavar="SLUG")


class Documents(FeatureResource):
    """The routes of the feature docs for one project, under /v1/x/docs/<project>/ (Gateway)."""

    feature = "docs"

    def list(self) -> list[dict]:
        return self.call("GET", "documents")

    def get(self, slug: str, version: int | None) -> dict:
        return self.call("GET", "documents", slug, version=version)

    def versions(self, slug: str) -> list[dict]:
        return self.call("GET", "documents", slug, "versions")

    def put(self, slug: str, body: dict) -> dict:
        return self.call("PUT", "documents", slug, body=body)

    def upload(self, path: Path, media_type: str) -> Any:
        return self.hub.upload(self.path("images"), path, media_type)

    def image(self, url: str) -> bytes:
        """The bytes of an image a version shows, at the URL the hub gave it."""
        return self.hub.get_bytes(url)


# Reading


def is_page(shown: dict) -> bool:
    return shown.get("format") == "html"


def write_out(documents: Documents, directory: Path, doc: dict) -> Path:
    shown = doc["shown"]
    directory.mkdir(parents=True, exist_ok=True)
    if is_page(shown):
        out = directory / f"{doc['slug']}.html"
        out.write_text(shown["body"], encoding="utf-8")
        return out
    for image in shown["images"]:
        target = directory / image["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(documents.image(image["url"]))
    out = directory / f"{doc['slug']}.md"
    out.write_text(f"# {shown['title']}\n\n{shown['body']}", encoding="utf-8")
    return out


class DocsList(HubCommand):
    name = "list"
    help = "the documents, with their latest version"
    arguments = (PROJECT,)
    output = returns_array(*SUMMARY_KEYS, schema="DocsDocumentSummary")

    def run(self, ctx: HubContext) -> Result:
        found = Documents.of(ctx).list()
        rows = [(d["slug"], d["latest_version"], d["updated_by"], when(d["updated_at"]), d["title"]) for d in found]
        empty = f"Project {ctx.project} has no document: `evo-agents hub docs put FILE --project {ctx.project}`."
        return Result(found, Table(("SLUG", "VERSION", "BY", "UPDATED", "TITLE"), rows, empty))


class DocsShow(HubCommand):
    name = "show"
    help = "a version as Markdown or an HTML page, or written to a directory"
    arguments = (
        SLUG_ARG,
        PROJECT,
        Arg("--version", type=int, metavar="N", help="this version instead of the latest"),
        Arg("--out", metavar="DIR", help="write SLUG.md and the images it shows, or SLUG.html, into DIR"),
    )
    output = returns_object(*DOCUMENT_KEYS, schema="DocsDocument")

    def run(self, ctx: HubContext) -> Result:
        args, documents = ctx.args, Documents.of(ctx)
        doc = documents.get(args.slug, args.version)
        if args.json:
            return Result(doc)
        shown = doc["shown"]
        if args.out:
            out = write_out(documents, Path(args.out), doc)
            count = len(shown["images"])
            return Result(
                doc, Text(f"Wrote version {shown['number']} of {doc['slug']} to {out}, with {count} image(s).")
            )
        text = shown["body"] if is_page(shown) else f"# {shown['title']}\n\n{shown['body']}"
        return Result(doc, Verbatim(text if text.endswith("\n") else text + "\n"))


class DocsHistory(HubCommand):
    name = "history"
    help = "every version of a document, the newest first"
    arguments = (SLUG_ARG, PROJECT)
    output = returns_array(*VERSION_KEYS, schema="DocsVersion")

    def run(self, ctx: HubContext) -> Result:
        found = Documents.of(ctx).versions(ctx.args.slug)
        rows = [(f"v{v['number']}", v["author"], when(v["created_at"]), v["summary"] or "-") for v in found]
        return Result(found, Table(("VERSION", "AUTHOR", "SAVED", "SUMMARY"), rows, bare=True))


# Saving


@dataclass
class Prepared:
    """A Markdown file ready to save: its title, its body with every local image named as the hub takes it, and the
    file of each name."""

    title: str | None
    body: str
    files: dict[str, Path] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


def local_path(src: str) -> str | None:
    """The relative path ``src`` names, decoded; None for a URL, an absolute path or an anchor."""
    src = src[1:-1] if src.startswith("<") and src.endswith(">") else src
    if not src or SCHEME.match(src) or src.startswith(("/", "#", "\\")):
        return None
    return unquote(src)


def cleaned(segment: str) -> str:
    found = re.sub(r"[^A-Za-z0-9._-]+", "-", segment).lstrip(".-")[:100]
    return found or "image"


def image_name(relative: str, taken: set[str]) -> str:
    """The name the hub takes for the image at ``relative``: the path itself when it is one, else its file's name
    cleaned under ``images/``, made unique among ``taken``."""
    path = PurePosixPath(relative.replace("\\", "/"))
    parts = [part for part in path.parts if part != "."]
    name = "/".join(parts)
    if IMAGE_NAME.fullmatch(name) and len(name) <= 200:
        return name
    stem = cleaned(path.name)
    name, number = f"images/{stem}", 2
    while name in taken:
        base, dot, ext = stem.rpartition(".")
        name = f"images/{base}-{number}.{ext}" if dot else f"images/{stem}-{number}"
        number += 1
    return name


def heading_title(lines: list[str]) -> tuple[str | None, list[str]]:
    """The title of the first line when it is a heading ``# Title``, and the lines without it."""
    first = next((at for at, line in enumerate(lines) if line.strip()), None)
    found = TITLE.match(lines[first]) if first is not None else None
    if first is None or found is None:
        return None, lines
    rest = lines[first + 1 :]
    while rest and not rest[0].strip():
        rest = rest[1:]
    return found["title"], rest


def prepare(file: Path, text: str) -> Prepared:
    """``text``, the Markdown of ``file``, with its local images named (see the module)."""
    title, lines = heading_title(text.splitlines(keepends=True))
    prepared = Prepared(title, "")
    by_file: dict[Path, str] = {}
    fenced = False

    def named(found: re.Match) -> str:
        relative = local_path(found["src"])
        if relative is None:
            return found[0]
        source = (file.parent / relative).resolve()
        if not source.is_file():
            prepared.missing.append(relative)
            return found[0]
        if source not in by_file:
            by_file[source] = image_name(relative, set(prepared.files))
            prepared.files[by_file[source]] = source
        return f"![{found['alt']}]({by_file[source]}{found['title'] or ''})"

    out = []
    for line in lines:
        if FENCE.match(line):
            fenced = not fenced
        out.append(line if fenced else IMAGE.sub(named, line))
    prepared.body = "".join(out)
    return prepared


def page_title(text: str) -> str | None:
    """The text of an HTML page's ``<title>``, its entities decoded and its spaces joined; None without one."""
    found = PAGE_TITLE.search(text)
    if found is None:
        return None
    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", found["title"]))).strip()
    return title or None


def slug_of(args: argparse.Namespace) -> str:
    slug = args.slug or re.sub(r"[^a-z0-9]+", "-", Path(args.file).stem.lower()).strip("-")[:100]
    if not SLUG.fullmatch(slug):
        raise ValueError(f"{slug!r} is no slug: lower-case letters, digits and dashes; pass --slug")
    return slug


def said(saved: dict) -> str:
    return "created" if saved["created"] else "saved" if saved["changed"] else "unchanged, nothing saved"


class DocsPut(HubCommand):
    name = "put"
    help = "save a Markdown file and the images beside it, or an .html page, as the next version"
    arguments = (
        Arg("file", metavar="FILE", help="the Markdown file, or an HTML page (.html, .htm)"),
        PROJECT,
        Arg("--slug", metavar="SLUG", help="the document's slug (default: from the file's name)"),
        Arg("--title", metavar="TITLE", help="its title (default: the first heading # Title, or <title>)"),
        Arg("--summary", metavar="TEXT", help="what this version changes"),
        Arg("--base", type=int, metavar="N", help="refuse the save unless the document is at version N"),
        Arg("--label", metavar="LEVEL", help="a new document's label level, an admin's choice"),
    )
    output = returns_object(*SAVED_KEYS, schema="DocsSaved")

    def run(self, ctx: HubContext) -> Result:
        args, file = ctx.args, Path(ctx.args.file)
        try:
            slug = slug_of(args)
            text = file.read_text(encoding="utf-8")
            if file.suffix.lower() in PAGE_SUFFIXES:
                return self.put_page(ctx, slug, text)
            prepared = prepare(file, text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise UsageError(str(exc)) from None
        if prepared.missing:
            raise UsageError(f"{file} shows images that are not there: {', '.join(prepared.missing)}")
        documents = Documents.of(ctx)
        shown = {
            name: documents.upload(path, MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"))["sha256"]
            for name, path in prepared.files.items()
        }
        body = {
            "title": args.title or prepared.title or slug,
            "body": prepared.body,
            "summary": args.summary or "",
            "images": [{"name": name, "sha256": sha256} for name, sha256 in shown.items()],
        }
        saved = self.save(documents, args, slug, body)
        return Result(saved, Text(f"{slug} version {saved['version']}: {said(saved)}, with {len(shown)} image(s)."))

    def put_page(self, ctx: HubContext, slug: str, text: str) -> Result:
        args = ctx.args
        title = args.title or page_title(text) or slug
        body = {"title": title, "body": text, "format": "html", "summary": args.summary or ""}
        saved = self.save(Documents.of(ctx), args, slug, body)
        return Result(saved, Text(f"{slug} version {saved['version']}: {said(saved)}, an HTML page."))

    @staticmethod
    def save(documents: Documents, args: argparse.Namespace, slug: str, body: dict) -> dict:
        if args.base is not None:
            body["base_version"] = args.base
        if args.label:
            body["label"] = {"level": args.label}
        return documents.put(slug, body)


DOCS = Group(
    "docs",
    "a project's documents: Markdown with images or HTML pages, each save a version (feature docs)",
    [DocsList(), DocsShow(), DocsHistory(), DocsPut()],
)
