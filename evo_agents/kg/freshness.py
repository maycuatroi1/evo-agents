"""Which commit each git source of a graph read, how far that commit is behind the remote's default branch, and the
links to the source at that commit.

The git connector ends a run with a cursor naming what it read (``evo_agents.kg.connectors.git.read_cursor``)::

    {"commit": "<sha>", "ref": "HEAD", "base": "origin/main", "behind": 3,
     "url": "https://github.com/o/r/commit/<sha>",
     "blob": {"branch": "https://github.com/o/r/blob/main/", "commit": "https://github.com/o/r/blob/<sha>/"}}

A build copies that read into the source's entry of its coverage (``add_reads``), so a graph says what it holds
wherever it is read. That includes the hub, which merges the run logs of every machine and has no checkout: there the
cursor of the newest run counts (``Corpus.record_cursor``). ``behind`` is counted when the source syncs, from the
clone's remote-tracking branch, without fetching; ``kg status`` and the SessionStart note count it again from the
checkout when this machine has one (``count_again``), still without the network. A source whose clone has no such
branch has no ``base`` and no ``behind``.

An item links to its file by branch (its ``uri``), because a link by commit would change every item's hash on every
commit. The tools show the link at the commit read instead (``Links``), so the lines of a span match what it opens.

Cursors come from connectors, and on the hub from any member's push: a value that does not look like a commit, a ref
or an https link is dropped, so no text of theirs reaches a tool's answer or the SessionStart note.
"""

from __future__ import annotations

import re

SHA = re.compile(r"[0-9a-f]{7,64}")
REF = re.compile(r"[A-Za-z0-9._/@^~{}-]{1,200}")
URL = re.compile(r"https://[^\s\"'<>]{1,500}")
SHORT = 7  # characters of a SHA shown
FIELDS = ("commit", "ref", "base", "behind", "url")  # what a read shows; ``blob`` stays inside the build


def _ref(value) -> str | None:
    return value if isinstance(value, str) and REF.fullmatch(value) else None


def _url(value) -> str | None:
    return value if isinstance(value, str) and URL.fullmatch(value) else None


def read_of(cursor) -> dict | None:
    """The read a source's cursor names, as the git connector writes it; None for any other cursor."""
    if not isinstance(cursor, dict) or not isinstance(cursor.get("commit"), str) or not SHA.fullmatch(cursor["commit"]):
        return None
    read = {"commit": cursor["commit"], "ref": _ref(cursor.get("ref")), "base": None, "behind": None}
    read["url"] = _url(cursor.get("url"))
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
    """The reads a build's coverage holds, one per source that has one: source, commit, ref, base, behind, url."""
    out = []
    for entry in (coverage or {}).get("sources", []):
        read = entry.get("read")
        if isinstance(read, dict) and isinstance(read.get("commit"), str):
            out.append({"source": entry["id"], **{key: read.get(key) for key in FIELDS}})
    return out


def count_again(project, found: list[dict]) -> list[dict]:
    """``found`` with ``behind`` counted again from each source's checkout on this machine as it is now; a source
    without a ``base`` or a checkout keeps the count of its sync. Never fetches."""
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
        out.append(read if count is None else {**read, "behind": count})
    return out


def _behind(read: dict) -> str | None:
    if read.get("base") and read.get("behind") is not None:
        return f"{read['behind']} behind {read['base']}"
    return None


def describe(read: dict) -> str:
    """One read in words: ``1550c7a of HEAD, 3 behind origin/main``."""
    behind = _behind(read)
    return f"{read['commit'][:SHORT]} of {read.get('ref') or 'HEAD'}" + (f", {behind}" if behind else "")


def brief(found: list[dict]) -> str | None:
    """The reads on one line, names and numbers only: ``commits read: app 1550c7a (3 behind origin/main)``."""
    if not found:
        return None
    parts = []
    for read in found:
        behind = _behind(read)
        parts.append(f"{read['source']} {read['commit'][:SHORT]}" + (f" ({behind})" if behind else ""))
    return "commits read: " + ", ".join(parts)


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
