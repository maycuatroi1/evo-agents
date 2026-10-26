"""``evo-agents hub docs list|show|history|put``: the documents of a project the hub's feature docs is on in, through its
routes under /v1/x/docs/<project>/ (``docs/hub.md``, section Docs). The feature's code is ``evo_agents.hub.server.docs``,
which this client never imports: it speaks HTTP only.

``list`` prints the documents with their latest version. ``show SLUG`` prints a version as Markdown, the latest unless
``--version N``, its title as the first heading; ``--out DIR`` writes it to ``DIR/<slug>.md`` instead, with every
image it shows under DIR by the name the Markdown gives it. ``history SLUG`` prints every version.

``put FILE`` saves a Markdown file as the next version of a document, created at version 1 when the project has none of
that slug (``--slug``, by default from the file's name). Every image the Markdown shows from a file beside it,
``![alt](shots/board.png "caption")``, is uploaded first, once each (the hub keeps an image once per project), and the
version names it by its path from the file; a path the hub does not take as a name (``../board.png``, spaces) is
renamed under ``images/`` and the Markdown with it. An image of the web (``https://...``) stays as it is, and the hub's
web does not load it. A local image that is not there stops the command before anything is sent. The title is
``--title``, else the file's first line when it is a heading ``# Title``, which then leaves the body, else the slug.
``--base N`` refuses the save unless the document is at version N; ``--label LEVEL`` gives a new document a label, an
admin's choice. ``--json`` prints what the hub answered. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
SUMMARY_KEYS = ("slug", "title", "latest_version", "label", "created_by", "created_at", "updated_by", "updated_at")
DOCUMENT_KEYS = (*SUMMARY_KEYS, "versions", "shown")
VERSION_KEYS = ("number", "title", "summary", "author", "created_at")
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
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _path(project: str, *parts: str, query: dict | None = None) -> str:
    path = "/v1/x/docs/" + "/".join(quote(str(part), safe="") for part in (project, *parts))
    given = {key: value for key, value in (query or {}).items() if value is not None}
    return f"{path}?{urlencode(given)}" if given else path


# Reading


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.project, "documents"))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {args.project} has no document: `evo-agents hub docs put FILE --project {args.project}`.")
        return 0
    rows = [(d["slug"], d["latest_version"], d["updated_by"], when(d["updated_at"]), d["title"]) for d in found]
    table(("SLUG", "VERSION", "BY", "UPDATED", "TITLE"), rows)
    return 0


def _write_out(hub, directory: Path, doc: dict) -> Path:
    shown = doc["shown"]
    directory.mkdir(parents=True, exist_ok=True)
    for image in shown["images"]:
        target = directory / image["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(hub.get_bytes(image["url"]))
    out = directory / f"{doc['slug']}.md"
    out.write_text(f"# {shown['title']}\n\n{shown['body']}", encoding="utf-8")
    return out


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    doc = hub.call("GET", _path(args.project, "documents", args.slug, query={"version": args.version}))
    if args.json:
        print_json(doc)
        return 0
    if args.out:
        out = _write_out(hub, Path(args.out), doc)
        count = len(doc["shown"]["images"])
        print(f"Wrote version {doc['shown']['number']} of {doc['slug']} to {out}, with {count} image(s).")
        return 0
    shown = doc["shown"]
    print(f"# {shown['title']}\n\n{shown['body']}", end="" if shown["body"].endswith("\n") else "\n")
    return 0


@client_command
def cmd_history(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.project, "documents", args.slug, "versions"))
    if args.json:
        print_json(found)
        return 0
    rows = [(f"v{v['number']}", v["author"], when(v["created_at"]), v["summary"] or "-") for v in found]
    table(("VERSION", "AUTHOR", "SAVED", "SUMMARY"), rows)
    return 0


# Saving


@dataclass
class Prepared:
    """A Markdown file ready to save: its title, its body with every local image named as the hub takes it, and the
    file of each name."""

    title: str | None
    body: str
    files: dict[str, Path] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)


def _local(src: str) -> str | None:
    """The relative path ``src`` names, decoded; None for a URL, an absolute path or an anchor."""
    src = src[1:-1] if src.startswith("<") and src.endswith(">") else src
    if not src or SCHEME.match(src) or src.startswith(("/", "#", "\\")):
        return None
    return unquote(src)


def _clean(segment: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", segment).lstrip(".-")[:100]
    return cleaned or "image"


def _name(relative: str, taken: set[str]) -> str:
    """The name the hub takes for the image at ``relative``: the path itself when it is one, else its file's name
    cleaned under ``images/``, made unique among ``taken``."""
    path = PurePosixPath(relative.replace("\\", "/"))
    parts = [part for part in path.parts if part != "."]
    name = "/".join(parts)
    if IMAGE_NAME.fullmatch(name) and len(name) <= 200:
        return name
    stem = _clean(path.name)
    name, number = f"images/{stem}", 2
    while name in taken:
        base, dot, ext = stem.rpartition(".")
        name = f"images/{base}-{number}.{ext}" if dot else f"images/{stem}-{number}"
        number += 1
    return name


def _title(lines: list[str]) -> tuple[str | None, list[str]]:
    """The title of the first line when it is a heading ``# Title``, and the lines without it."""
    first = next((at for at, line in enumerate(lines) if line.strip()), None)
    found = TITLE.match(lines[first]) if first is not None else None
    if found is None:
        return None, lines
    rest = lines[first + 1 :]
    while rest and not rest[0].strip():
        rest = rest[1:]
    return found["title"], rest


def prepare(file: Path, text: str) -> Prepared:
    """``text``, the Markdown of ``file``, with its local images named (see the module)."""
    title, lines = _title(text.splitlines(keepends=True))
    prepared = Prepared(title, "")
    by_file: dict[Path, str] = {}
    fenced = False

    def named(found: re.Match) -> str:
        relative = _local(found["src"])
        if relative is None:
            return found[0]
        source = (file.parent / relative).resolve()
        if not source.is_file():
            prepared.missing.append(relative)
            return found[0]
        if source not in by_file:
            by_file[source] = _name(relative, set(prepared.files))
            prepared.files[by_file[source]] = source
        return f"![{found['alt']}]({by_file[source]}{found['title'] or ''})"

    out = []
    for line in lines:
        if FENCE.match(line):
            fenced = not fenced
        out.append(line if fenced else IMAGE.sub(named, line))
    prepared.body = "".join(out)
    return prepared


def _slug(args) -> str:
    slug = args.slug or re.sub(r"[^a-z0-9]+", "-", Path(args.file).stem.lower()).strip("-")[:100]
    if not SLUG.fullmatch(slug):
        raise ValueError(f"{slug!r} is no slug: lower-case letters, digits and dashes; pass --slug")
    return slug


def _upload(hub, project: str, files: dict[str, Path]) -> dict[str, str]:
    """Each file uploaded once; {name: sha256}."""
    sha256 = {}
    for name, path in files.items():
        media_type = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        sha256[name] = hub.upload(_path(project, "images"), path, media_type)["sha256"]
    return sha256


@client_command
def cmd_put(args) -> int:
    file = Path(args.file)
    try:
        slug = _slug(args)
        prepared = prepare(file, file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if prepared.missing:
        print(f"error: {file} shows images that are not there: {', '.join(prepared.missing)}", file=sys.stderr)
        return EXIT_USAGE
    hub, _ = signed_in()
    shown = _upload(hub, args.project, prepared.files)
    body = {
        "title": args.title or prepared.title or slug,
        "body": prepared.body,
        "summary": args.summary or "",
        "images": [{"name": name, "sha256": sha256} for name, sha256 in shown.items()],
    }
    if args.base is not None:
        body["base_version"] = args.base
    if args.label:
        body["label"] = {"level": args.label}
    saved = hub.call("PUT", _path(args.project, "documents", slug), body)
    if args.json:
        print_json(saved)
        return 0
    what = "created" if saved["created"] else "saved" if saved["changed"] else "unchanged, nothing saved"
    print(f"{slug} version {saved['version']}: {what}, with {len(shown)} image(s).")
    return 0


def _project(parser) -> None:
    parser.add_argument("--project", required=True, metavar="PROJECT", help="the project on the hub")


def register_docs(hsub) -> None:
    docs = hsub.add_parser(
        "docs", help="a project's documents: Markdown with images, each save a version (feature docs)"
    )
    dsub = docs.add_subparsers(dest="docs_command", required=True)

    listed = dsub.add_parser("list", help="the documents, with their latest version")
    _project(listed)
    json_option(listed, returns_array(*SUMMARY_KEYS, schema="DocsDocumentSummary"))
    listed.set_defaults(func=cmd_list)

    show = dsub.add_parser("show", help="a version as Markdown, or written to a directory with its images")
    show.add_argument("slug", metavar="SLUG")
    _project(show)
    show.add_argument("--version", type=int, metavar="N", help="this version instead of the latest")
    show.add_argument("--out", metavar="DIR", help="write SLUG.md and the images it shows into DIR")
    json_option(show, returns_object(*DOCUMENT_KEYS, schema="DocsDocument"))
    show.set_defaults(func=cmd_show)

    history = dsub.add_parser("history", help="every version of a document, the newest first")
    history.add_argument("slug", metavar="SLUG")
    _project(history)
    json_option(history, returns_array(*VERSION_KEYS, schema="DocsVersion"))
    history.set_defaults(func=cmd_history)

    put = dsub.add_parser("put", help="save a Markdown file and the images beside it as the next version")
    put.add_argument("file", metavar="FILE", help="the Markdown file")
    _project(put)
    put.add_argument("--slug", metavar="SLUG", help="the document's slug (default: from the file's name)")
    put.add_argument("--title", metavar="TITLE", help="its title (default: the file's first heading # Title)")
    put.add_argument("--summary", metavar="TEXT", help="what this version changes")
    put.add_argument("--base", type=int, metavar="N", help="refuse the save unless the document is at version N")
    put.add_argument("--label", metavar="LEVEL", help="a new document's label level, an admin's choice")
    json_option(put, returns_object(*SAVED_KEYS, schema="DocsSaved"))
    put.set_defaults(func=cmd_put)
