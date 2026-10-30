"""Which commit each git source of a graph read, how far that commit is behind the remote's default branch, and the
links to the source at that commit.

The git connector ends a run with a cursor naming what it read (``evo_agents.kg.connectors.git.read_cursor``)::

    {"commit": "<sha>", "ref": "HEAD", "at": "2026-10-30T13:54:33Z", "base": "origin/main", "behind": 3,
     "url": "https://github.com/o/r/commit/<sha>",
     "blob": {"branch": "https://github.com/o/r/blob/main/", "commit": "https://github.com/o/r/blob/<sha>/"}}

A build copies that read into the source's entry of its coverage (``add_reads``), so a graph says what it holds
wherever it is read. That includes the hub, which merges the run logs of every machine and has no checkout: there the
cursor of the newest run counts (``Corpus.record_cursor``). ``behind`` is counted when the source syncs, from the
clone's remote-tracking branch, without fetching; ``at`` says when, so a tool says the count is of then
(``describe``): a sync that finds the ref still at the commit read keeps no log, and the hub has no newer count to
show. ``kg status`` and the SessionStart note count it again from the checkout when this machine has one
(``count_again``), still without the network. A source whose clone has no such branch has no ``base`` and no
``behind``; a cursor written before ``at`` existed has none either.

An item links to its file by branch (its ``uri``), because a link by commit would change every item's hash on every
commit. The tools show the link at the commit read instead (``Links``), so the lines of a span match what it opens.

A source the hub's core plugins keep (``evo_agents.hub.kg_sources``) may end with a cursor naming the newest item it
gave, ``{"item": "<id>", "at": "<time>"}``, such as the newest message of the chats; a build copies it as the
source's read too, which kg_status shows (``describe``) and ``reads`` leaves out, since it names no commit.

Cursors come from connectors, and on the hub from any member's push: a value that does not look like a commit, a ref
or an https link is dropped, so no text of theirs reaches a tool's answer or the SessionStart note; an item's cursor
keeps only an id and a time that look like one.
"""

from __future__ import annotations

import re

from evo_agents.kg.protocol import ID_PATTERN, TIME_PATTERN

SHA = re.compile(r"[0-9a-f]{7,64}")
ITEM = re.compile(ID_PATTERN)
TIME = re.compile(TIME_PATTERN)
REF = re.compile(r"[A-Za-z0-9._/@^~{}-]{1,200}")
URL = re.compile(r"https://[^\s\"'<>]{1,500}")
SHORT = 7  # characters of a SHA shown
FIELDS = ("commit", "ref", "at", "base", "behind", "url")  # what a read shows; ``blob`` stays inside the build


def _ref(value) -> str | None:
    return value if isinstance(value, str) and REF.fullmatch(value) else None


def _url(value) -> str | None:
    return value if isinstance(value, str) and URL.fullmatch(value) else None


def _item_read(cursor: dict) -> dict | None:
    item, at = cursor.get("item"), cursor.get("at")
    if isinstance(item, str) and ITEM.match(item) and isinstance(at, str) and TIME.match(at):
        return {"item": item, "at": at}
    return None


def read_of(cursor) -> dict | None:
    """The read a source's cursor names, as the git connector writes it, or the newest item a plugin's source gave;
    None for any other cursor."""
    if isinstance(cursor, dict) and "item" in cursor:
        return _item_read(cursor)
    if not isinstance(cursor, dict) or not isinstance(cursor.get("commit"), str) or not SHA.fullmatch(cursor["commit"]):
        return None
    read = {"commit": cursor["commit"], "ref": _ref(cursor.get("ref")), "base": None, "behind": None}
    read["url"] = _url(cursor.get("url"))
    at = cursor.get("at")
    if isinstance(at, str) and TIME.match(at):
        read["at"] = at
    behind = cursor.get("behind")
    if _ref(cursor.get("base")) and type(behind) is int and behind >= 0:
        read.update(base=cursor["base"], behind=behind)
    blob = cursor.get("blob") if isinstance(cursor.get("blob"), dict) else {}
    if _url(blob.get("branch")) and _url(blob.get("commit")):
        read["blob"] = {"branch": blob["branch"], "commit": blob["commit"]}
    return read


def add_reads(coverage: dict, corpus) -> None:
    """Put into each source's entry of a build's coverage the read its cursor in ``corpus`` names, if any."""
    for entry in coverage.get("sources", []):
        read = read_of(corpus.cursor(entry["id"]))
        if read is not None:
            entry["read"] = read


def reads(coverage: dict | None) -> list[dict]:
    """The reads a build's coverage holds, one per source that has one: source, commit, ref, at, base, behind, url
    (``at`` None for a read older than the field)."""
    out = []
    for entry in (coverage or {}).get("sources", []):
        read = entry.get("read")
        if isinstance(read, dict) and isinstance(read.get("commit"), str):
            out.append({"source": entry["id"], **{key: read.get(key) for key in FIELDS}})
    return out


def count_again(project, found: list[dict]) -> list[dict]:
    """``found`` with ``behind`` counted again from each source's checkout on this machine as it is now, marked
    ``recounted``; a source without a ``base`` or a checkout keeps the count of its sync. Never fetches."""
    from evo_agents.kg.connectors.git import behind_now
    from evo_agents.kg.project import ProjectError
    from evo_agents.kg.protocol.runner import ConnectorContext

    out = []
    for read in found:
        count = None
        if read.get("base"):
            try:
                ctx = ConnectorContext(project.name, project.source(read["source"]), project.harness)
            except ProjectError:
                ctx = None
            count = behind_now(ctx, read["commit"], read["base"]) if ctx is not None else None
        out.append(read if count is None else {**read, "behind": count, "recounted": True})
    return out


def _behind(read: dict) -> str | None:
    if read.get("base") and read.get("behind") is not None:
        return f"{read['behind']} behind {read['base']}"
    return None


def describe(read: dict) -> str:
    """One read in words: ``1550c7a of HEAD at <time>, 3 behind origin/main then`` when the count is the sync's,
    without ``then`` when this machine counted it again, without the time for a read older than ``at``; or
    ``plugin.chats:message:812 at <time>``."""
    if "commit" not in read:
        return f"{read.get('item')} at {read.get('at')}"
    at = read.get("at")
    behind = _behind(read)
    if behind and at and not read.get("recounted"):
        behind += " then"
    return (
        f"{read['commit'][:SHORT]} of {read.get('ref') or 'HEAD'}"
        + (f" at {at}" if at else "")
        + (f", {behind}" if behind else "")
    )


def brief(found: list[dict]) -> str | None:
    """The reads on one line for the SessionStart note, names and numbers only. It counts the sources and names
    only those not known to be current, since the note reaches every session: ``commits read: 16 sources, 13
    current; behind: app 1550c7a (3 behind origin/main)``, and ``commits read: 16 sources, all current`` when none
    is behind. A source whose clone had no branch to count against is listed under ``behind unknown``."""
    if not found:
        return None
    behind = [r for r in found if _behind(r) and r["behind"] > 0]
    unknown = [r for r in found if not _behind(r)]
    current = len(found) - len(behind) - len(unknown)
    parts = [
        f"commits read: {len(found)} source{'' if len(found) == 1 else 's'}, "
        + ("all current" if current == len(found) else f"{current} current")
    ]
    if behind:
        parts.append("behind: " + ", ".join(f"{r['source']} {r['commit'][:SHORT]} ({_behind(r)})" for r in behind))
    if unknown:
        parts.append("behind unknown: " + ", ".join(f"{r['source']} {r['commit'][:SHORT]}" for r in unknown))
    return "; ".join(parts)


class Links:
    """Turns a source's file link by branch into the link at the commit the source read, from a build's coverage."""

    def __init__(self, coverage: dict | None):
        self.prefixes: dict[str, tuple[str, str]] = {}
        for entry in (coverage or {}).get("sources", []):
            blob = (entry.get("read") or {}).get("blob")
            if isinstance(blob, dict) and _url(blob.get("branch")) and _url(blob.get("commit")):
                self.prefixes[entry["id"]] = (blob["branch"], blob["commit"])

    @classmethod
    def of(cls, store, b: int) -> Links:
        return cls(store.build_row(b)["coverage"])

    def at_commit(self, uri, source) -> str | None:
        """``uri`` at the commit its source read; any other link as it is."""
        prefixes = self.prefixes.get(source) if isinstance(source, str) else None
        if not isinstance(uri, str) or prefixes is None or not uri.startswith(prefixes[0]):
            return uri
        return prefixes[1] + uri[len(prefixes[0]) :]

    def node(self, n: dict) -> dict:
        """Node ``n`` with its ``uri`` at the commit its source read."""
        props = n["props"]
        uri = self.at_commit(props.get("uri"), props.get("source"))
        return n if uri == props.get("uri") else {**n, "props": {**props, "uri": uri}}
