"""What the graph links to the lines a grep matched: the lines of the note ``evo-agents kg hook post-search`` adds
(``evo_agents.kg.post_search``), which the hub answers from the snapshot of the project's graph it holds
(POST /v1/kg/{project}/grep-links, ``evo_agents.hub.server.kg_grep``).

The hook sends the matched lines, each as (its file as (repo, path), its line number or None for the file alone, the
matched text); ``link_lines`` reads them against a ``SnapshotView`` as one reader reads it, with that reader's
(level, location) clearance. A file is found by its path among the File and Document nodes the reader sees; a matched
line falls in a symbol of it only when the graph agrees (``FileSymbols``).

At most MAX_NODES lines come back, one per matched node that has something to add: a symbol whose definition line
matched comes first, then the other symbols with a matched line, then files matched outside any symbol (or named by
``-l``). Each line names MAX_SHOWN callers, tests and plan steps at most and counts the rest. A line carries only node
ids that the parser derived from paths and code names and that SAFE takes, never a name, heading or other text of a
source (integrity U), and only nodes and edges of a status in STATUSES whose label the clearance and the reader's
audience hold. The matched text only places a line in a symbol; it never comes back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evo_agents.kg.refs import readable

MAX_FILES = 100  # files of one request
MAX_HITS = 5000  # matched lines of one request, as many as the hook reads of a result
MAX_TEXT = 400  # characters of a matched line used; enough for the definition or the call it holds
MAX_NODES = 4  # lines of an answer
MAX_SHOWN = 2  # callers, tests and plan steps one line names; the rest are counted
MAX_EDGES = 300  # edges read into one node
STATUSES = ("declared", "parsed", "introspected", "resolved")  # never proposed
STEP_RELS = ("mentions", "touches", "implements", "verifies")
OPEN_STEPS = ("pending", "in_progress", "blocked")
SAFE = re.compile(r"[A-Za-z0-9._:/#@-]{1,160}")  # what an id may look like to be printed at all
# A line that defines a name, in the languages the graph reads: def, class, function, func (Go methods too), fn,
# interface, struct, enum, trait, type, record, object, module, and const, let or var bound to something.
DEFINES = re.compile(
    r"\b(?:def|class|function|func|fn|interface|struct|enum|trait|type|record|object|module)\s+"
    r"(?:\([^)]*\)\s*)?(?:[\w$]+\.)*([\w$]+)|\b(?:const|let|var)\s+([\w$]+)\s*="
)
SPAN = re.compile(r"lines? (\d+)(?:-(\d+))?")
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|specs?)/"
    r"|(?:^|/)(?:test_[^/]*|conftest\.py|[^/]*_test\.\w+|[^/]*\.(?:test|spec)\.\w+|[^/]*Tests?\.\w+)$"
)

Key = tuple[str, str]  # (repo, path inside it)
Hit = tuple[Key, int | None, str]  # a matched line: its file, its line number (None: the file only), its text


@dataclass
class Match:
    """A node a matched line falls in: a symbol, or a file when the line is outside every symbol or the result named
    only the file. ``order`` is where its first line came in the result."""

    node: dict
    file: dict
    order: int
    defined: bool = False


def is_test(path) -> bool:
    return isinstance(path, str) and TEST_PATH.search(path) is not None


# -- the graph -------------------------------------------------------------------------------------


def file_nodes(view, wanted: set[Key]) -> dict[Key, dict]:
    """The File and Document nodes of these (repo, path) pairs that the reader sees, through the index on names (a
    file's name is its path)."""
    ids: set[str] = set()
    for path in sorted({path for _, path in wanted}):
        ids.update(view.exact(path, limit=10_000, kinds=("File", "Document")))
    out = {}
    for node in view.nodes(sorted(ids)).values():
        key = (node["props"].get("repo"), node["props"].get("path"))
        if key in wanted:
            out[key] = node
    return out


def symbols_of(view, file_id: str) -> list[dict]:
    """The Symbol nodes a file defines that the reader sees, through the order of ids:
    ``symbol:<source>:<path>::<qualname>``."""
    source, sep, path = file_id.partition(":file:")
    if not sep:
        return []
    prefix = f"symbol:{source}:{path}::"
    ids = [node_id for node_id, _ in view.nodes_between(prefix, prefix + "\U0010ffff")]
    return [n for n in view.nodes(ids).values() if n["kind"] == "Symbol" and n["props"].get("file") == file_id]


def extents(spans: list[str]) -> list[tuple[int, int]]:
    """The line spans (``lines a-b``) of a file's top-level definitions, from the spans of its units."""
    out = []
    for span in spans:
        found = SPAN.fullmatch(span or "")
        if found and found[2]:
            out.append((int(found[1]), int(found[2])))
    return out


def symbol_at(line: int, symbols: list[dict], spans: list[tuple[int, int]]) -> dict | None:
    """The innermost symbol a line falls in: inside the top-level definition around it, the symbol that starts last
    at or before the line (the definition itself for a decorator line)."""
    around = [start for start, end in spans if start <= line <= end]
    if not around:
        return None
    top = max(around)
    inside = [s for s in symbols if isinstance(s["props"].get("line"), int) and s["props"]["line"] >= top]
    before = [s for s in inside if s["props"]["line"] <= line]
    if before:
        return max(before, key=lambda s: s["props"]["line"])
    return min(inside, key=lambda s: s["props"]["line"], default=None)


def short_name(symbol_id: str) -> str:
    """The last part of a symbol's qualified name: ``search`` for ``symbol:r:p.py::SnapshotView.search``."""
    return symbol_id.rpartition("::")[2].rpartition(".")[2]


class FileSymbols:
    """The symbols of one file and where they start, as the graph read it. The checkout may differ from the commit
    the graph read, so a line number alone never places a match: a line that defines a name goes to the symbol of
    that name, and any other line to the symbol around its number only when that symbol calls a name the line
    holds. When a definition sits elsewhere than the graph has it, the file has moved, and no other line of it is
    placed by number."""

    def __init__(self, view, file_id: str, spans: list[str], visible):
        self.view, self.visible = view, visible
        self.symbols = [s for s in symbols_of(view, file_id) if isinstance(s["props"].get("line"), int)]
        self.spans = extents(spans)
        self.by_name: dict[str, list[dict]] = {}
        for symbol in self.symbols:
            self.by_name.setdefault(short_name(symbol["id"]), []).append(symbol)
        self.callees: dict[str, set[str]] = {}
        self.moved = False

    def definition(self, number: int, text: str) -> dict | None:
        """The symbol a matched line defines, by the name it defines; the nearest one when several share it."""
        found = DEFINES.search(text or "")
        named = self.by_name.get(found[1] or found[2]) if found else None
        if not named:
            return None
        symbol = min(named, key=lambda s: (abs(s["props"]["line"] - number), s["id"]))
        self.moved = self.moved or symbol["props"]["line"] != number
        return symbol

    def around(self, number: int, text: str) -> tuple[dict | None, bool]:
        """The symbol a line falls in, if the graph agrees, and whether the line is where it starts."""
        symbol = None if self.moved else symbol_at(number, self.symbols, self.spans)
        if symbol is None:
            return None, False
        names = set(IDENT.findall(text or ""))
        if symbol["props"]["line"] == number and short_name(symbol["id"]) in names:
            return symbol, True  # a definition without a keyword, such as a method of a TypeScript class
        if symbol["id"] not in self.callees:
            edges = self.view.edges_of(symbol["id"], 0, "out", limit=MAX_EDGES)
            calls = [e for e in edges if e["rel"] == "calls" and self.visible(e["label"])]
            self.callees[symbol["id"]] = {short_name(e["dst"]) for e in calls}
        return (symbol, False) if names & self.callees[symbol["id"]] else (None, False)


def find_matches(view, hits: list[Hit], files: dict[Key, dict], visible) -> list[Match]:
    """The visible nodes the hits fall in, best first: symbols whose definition matched, other symbols, then files."""
    numbered = {files[key]["id"] for key, number, _ in hits if number is not None and key in files}
    spans = view.unit_spans(numbered, visible) if numbered else {}
    per_file: dict[str, FileSymbols] = {}
    placed = []
    for order, (key, number, text) in enumerate(hits):
        file = files.get(key)
        if file is None or not visible(file["label"]):
            continue
        symbols = None
        if number is not None and file["kind"] == "File":
            if file["id"] not in per_file:
                per_file[file["id"]] = FileSymbols(view, file["id"], spans.get(file["id"], []), visible)
            symbols = per_file[file["id"]]
        placed.append((order, file, number, text, symbols, symbols.definition(number, text) if symbols else None))
    matches: dict[str, Match] = {}
    for order, file, number, text, symbols, node in placed:
        defined = node is not None
        if node is None and symbols is not None:
            node, defined = symbols.around(number, text)
        if node is None or not visible(node["label"]):
            node, defined = file, False
        match = matches.setdefault(node["id"], Match(node, file, order))
        match.defined = match.defined or defined
    return sorted(matches.values(), key=lambda m: (m.node is m.file, not m.defined, m.order))


# -- what links to them ----------------------------------------------------------------------------


class Links:
    """Reads the edges into matched nodes once per node, under the reader's (level, location) clearance."""

    def __init__(self, view, ceiling: tuple[int, int]):
        self.view, self.ceiling = view, ceiling
        self.cache: dict[tuple[str, tuple], list[dict]] = {}

    def visible(self, label) -> bool:
        return label[0] <= self.ceiling[0] and label[1] <= self.ceiling[1]

    def into(self, node_id: str, rels: tuple) -> list[dict]:
        """The visible nodes with an edge of ``rels`` into ``node_id``, by id."""
        key = (node_id, rels)
        if key not in self.cache:
            edges = self.view.edges_into(node_id, 0, rels, STATUSES, self.ceiling, limit=MAX_EDGES)
            sources = self.view.nodes(sorted({e["src"] for e in edges if e["src"] != node_id}))
            self.cache[key] = sorted((n for n in sources.values() if self.visible(n["label"])), key=lambda n: n["id"])
        return self.cache[key]

    def of(self, match: Match) -> tuple[list[dict], list[dict], list[dict]]:
        """(callers or importers, tests, plan steps) of a matched node."""
        importers = self.into(match.file["id"], ("imports",))
        steps = [n for n in self.into(match.file["id"], STEP_RELS) if n["kind"] == "PlanStep"]
        tests = [n for n in importers if is_test(n["props"].get("path"))]
        if match.node is match.file:
            return [n for n in importers if not is_test(n["props"].get("path"))], tests, steps
        callers = self.into(match.node["id"], ("calls",))
        own = [n for n in self.into(match.node["id"], STEP_RELS) if n["kind"] == "PlanStep"]
        tested = [n for n in callers if is_test(n["props"].get("path"))]
        covered = {n["props"].get("file") for n in tested}
        tests = tested + [n for n in tests if n["id"] not in covered]
        steps = own + [n for n in steps if n["id"] not in {s["id"] for s in own}]
        return [n for n in callers if not is_test(n["props"].get("path"))], tests, steps


def _stem(path) -> str:
    name = path.rsplit("/", 1)[-1] if isinstance(path, str) else ""
    return name.split(".", 1)[0]


def rank_tests(tests: list[dict], path) -> list[dict]:
    """Tests named after the file first (test_store.py for store.py), test functions before test files."""
    stem = _stem(path)

    def key(n: dict):
        own = _stem(n["props"].get("path")).removeprefix("test_").removesuffix("_test") == stem
        return (not own, n["kind"] != "Symbol", n["id"])

    return sorted(tests, key=key)


def rank_steps(steps: list[dict]) -> list[dict]:
    """Steps not done first, then the most recently done."""
    done = sorted(steps, key=lambda n: str(n["props"].get("done_at") or ""), reverse=True)
    return sorted(done, key=lambda n: n["props"].get("status") not in OPEN_STEPS)


def named(nodes: list[dict]) -> tuple[list[str], int]:
    """The readable ids of the first MAX_SHOWN nodes that can be printed, and how many nodes are left out."""
    ids = [rid for rid in (readable(n["id"]) for n in nodes) if SAFE.fullmatch(rid)]
    return ids[:MAX_SHOWN], len(nodes) - len(ids[:MAX_SHOWN])


def render_line(match: Match, callers: list[dict], tests: list[dict], steps: list[dict]) -> str | None:
    parts = []
    called = "imported by" if match.node is match.file else "callers"
    path = match.file["props"].get("path")
    for label, nodes in ((called, callers), ("tests", rank_tests(tests, path)), ("plan steps", rank_steps(steps))):
        ids, rest = named(nodes)
        if ids:
            parts.append(f"{label} {', '.join(ids)}" + (f" (+{rest} more)" if rest else ""))
    own = readable(match.node["id"])
    if not parts or not SAFE.fullmatch(own):
        return None
    return f"- {own}: " + "; ".join(parts)


def link_lines(view, ceiling: tuple[int, int] | None, hits: list[Hit]) -> list[str]:
    """The lines of the note for ``hits`` (the first MAX_HITS, of the first MAX_FILES files they name), as a reader of
    ``view`` with the (level, location) clearance ``ceiling`` reads the graph; [] for no clearance."""
    if ceiling is None:
        return []  # a sink the policy does not declare sees nothing
    wanted: dict[Key, None] = {}
    for key, _, _ in hits[:MAX_HITS]:
        if len(wanted) < MAX_FILES:
            wanted.setdefault(key)
    hits = [(key, number, (text or "")[:MAX_TEXT]) for key, number, text in hits[:MAX_HITS] if key in wanted]
    files = file_nodes(view, set(wanted))
    if not files:
        return []
    links = Links(view, ceiling)
    lines: list[str] = []
    shown_files: set[str] = set()
    for match in find_matches(view, hits, files, links.visible):
        if len(lines) >= MAX_NODES:
            break
        if match.node is match.file and match.file["id"] in shown_files:
            continue  # a symbol of this file already names its importers' tests and steps
        line = render_line(match, *links.of(match))
        if line:
            lines.append(line)
            shown_files.add(match.file["id"])
    return lines
