"""``evo-agents kg hook post-search``: after a Grep, or an rg or grep command in Bash, add what the project graph
links to the lines it matched: who calls the function or method a matched line falls in, the tests that call it or
import its file, and the plan steps that name it or its file.

The search runs first and its output reaches the model as it is; the hook only adds lines, through
``additionalContext``, and never sets ``updatedToolOutput``. So when the PostToolUse hook of evo-hub cuts a long result
down to its head and tail (``evo_agents.hub.spool``), what this hook adds stays whole, and it reads the result as Claude
Code hands it to every hook, before that cut. The note reaches the model with the authority of a system reminder, so it
carries only node ids that the parser derived from paths and code names, never a name, heading or other text of a
source (integrity U), and only nodes and edges whose label the sink may see.

The checkout may differ from the commit the graph read, so a line number alone never places a match in a symbol
(``FileSymbols``). The hook runs as ``evo-agents kg hook post-search``, which skips the parsers of every other command
(``evo_agents.cli.main``): on a laptop on 2026-10-18, building them took 0.2 to 0.3 s, and ``uvx`` itself 0.03 s.

At most MAX_NODES lines follow the header, one per matched node that has something to add: a symbol whose definition
line matched comes first, then the other symbols with a matched line, then files matched outside any symbol (or named
by ``-l``). Each line names MAX_SHOWN callers, tests and plan steps at most and counts the rest. Nothing to add, an
unbound directory, no store, a lookup past BUDGET or a command whose output holds no matches print nothing. The hook
reads only the store: no source file, no git, no network.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from evo_agents.kg import grep_command
from evo_agents.kg.project import ProjectError, resolve_project
from evo_agents.kg.refs import readable
from evo_agents.kg.store import LIVE, Store

SINK = "claude-code@anthropic"
BUDGET = 0.35  # seconds for reading the output and the store; past it the hook says nothing
MAX_NODES = 4  # lines after the header
MAX_SHOWN = 2  # callers, tests and plan steps one line names; the rest are counted
MAX_OUTPUT_LINES = 5000  # lines of a result read
MAX_FILES = 100  # files of a result looked up
MAX_EDGES = 300  # edges read into one node
STATUSES = ("declared", "parsed", "introspected", "resolved")  # never proposed
STEP_RELS = ("mentions", "touches", "implements", "verifies")
OPEN_STEPS = ("pending", "in_progress", "blocked")
SAFE = re.compile(r"[A-Za-z0-9._:/#@-]{1,160}")  # what an id may look like to be printed at all
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PREFIXED = re.compile(r"(.+?):(\d+):")  # path:line:text
FIRST_COLON = re.compile(r"([^:]+):")  # path:text, path:count
BARE = re.compile(r"(\d+)(?::|$)")  # line:text of a single file or under a heading; a count alone
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
OFF = ("0", "false", "off", "no")
NOTE = (
    "evo-kg: what the project graph links to the code this search matched (callers, tests, plan steps); "
    "kg_context or kg_impact with an id gives more:"
)


class Timeout(Exception):
    pass


@dataclass
class Output:
    """The lines of a search result and what reading them needs: the directory relative paths start from, the path
    arguments (one file means its lines carry no path), and whether lines carry line numbers (None: as they look)."""

    lines: list[str]
    base: str | None
    paths: list[str] = field(default_factory=list)
    numbered: bool | None = None
    count: bool = False


@dataclass
class Match:
    """A node a matched line falls in: a symbol, or a file when the line is outside every symbol or the result named
    only the file. ``order`` is where its first line came in the result."""

    node: dict
    file: dict
    order: int
    defined: bool = False


def enabled(env=None) -> bool:
    value = (os.environ if env is None else env).get("EVO_KG_GREP_HINTS", "")
    return value.strip().lower() not in OFF


def is_test(path) -> bool:
    return isinstance(path, str) and TEST_PATH.search(path) is not None


def _check(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise Timeout


# -- the result ------------------------------------------------------------------------------------


def read_output(event: dict, only: str | None = None) -> Output | None:
    """The result of a PostToolUse event for Grep or for rg or grep in Bash; None for anything else."""
    tool, given, response = event.get("tool_name"), event.get("tool_input"), event.get("tool_response")
    if not isinstance(given, dict) or not isinstance(response, dict):
        return None
    cwd = event.get("cwd")
    base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    if tool == "Bash":
        call = grep_command.parse(given.get("command"), base, only)
        stdout = response.get("stdout")
        if call is None or not isinstance(stdout, str) or response.get("interrupted"):
            return None
        return Output(_lines(stdout), call.base, call.paths, call.numbered, call.count)
    if tool == "Grep":
        path = given.get("path")
        paths = [path] if isinstance(path, str) and path else []
        content, names = response.get("content"), response.get("filenames")
        if isinstance(content, str) and content:
            return Output(_lines(content), base, paths, None, response.get("mode") == "count")
        if isinstance(names, list):
            return Output([n for n in names[:MAX_OUTPUT_LINES] if isinstance(n, str)], base, paths)
    return None


def _lines(text: str) -> list[str]:
    return [ANSI.sub("", line) for line in text.split("\n", MAX_OUTPUT_LINES)[:MAX_OUTPUT_LINES] if line]


@dataclass
class Line:
    """One line of a result, read every way it may be: the path before its first colon (or before ``:N:``) with the
    line number and the matched text after it, the whole line as a path (``-l``, a heading), and a bare number with
    its text (a line of a single file, or under a heading)."""

    whole: str
    path: str | None = None
    number: int | None = None
    text: str = ""
    bare: int | None = None
    bare_text: str = ""


def parse_line(text: str, numbered: bool | None, count: bool) -> Line | None:
    line = Line(text)
    bare = BARE.match(text)
    if bare:  # a number first is a line number (or a count), never a file name
        if count and int(bare[1]) == 0:
            return None
        if numbered is not False or count:
            line.bare, line.bare_text = int(bare[1]), text[bare.end() :]
        return line
    found = PREFIXED.match(text) if numbered is not False and not count else None
    if found:
        line.path, line.number, line.text = found[1], int(found[2]), text[found.end() :]
        return line
    found = FIRST_COLON.match(text)
    if found:
        line.path, line.text = found[1], text[found.end() :]
        if count and line.text.strip() == "0":
            return None  # grep -c names files without a match too
    return line


# -- where its paths point -------------------------------------------------------------------------


def repo_roots(project) -> list[tuple[str, str]]:
    """(repo name, real directory) of the harness and each repo with a checkout, longest directory first: the
    name a file node's ``props.repo`` carries."""
    harness = project.harness
    roots = [(harness.root.name, os.path.realpath(harness.root))]
    for repo in harness.repos():
        path = harness.repo_path(repo) if isinstance(repo, dict) and isinstance(repo.get("name"), str) else None
        if path is not None:
            roots.append((repo["name"], os.path.realpath(path)))
    return sorted(roots, key=lambda r: -len(r[1]))


class Paths:
    """Maps a path as a result writes it to (repo, path inside it)."""

    def __init__(self, roots: list[tuple[str, str]], base: str | None):
        self.roots = roots
        self.base = os.path.realpath(base) if base else None
        self.seen: dict[str, tuple[str, str] | None] = {}

    def __call__(self, raw: str | None) -> tuple[str, str] | None:
        if not raw or len(raw) > 1000:
            return None
        if raw not in self.seen:
            self.seen[raw] = self._resolve(raw)
        return self.seen[raw]

    def _resolve(self, raw: str) -> tuple[str, str] | None:
        target = grep_command.native(raw.strip())
        if not os.path.isabs(target):
            if not self.base:
                return None
            target = os.path.join(self.base, target)
        target = os.path.normpath(target)
        for name, root in self.roots:
            if target.startswith(root + os.sep):
                return name, os.path.relpath(target, root).replace(os.sep, "/")
        return None


# -- the graph -------------------------------------------------------------------------------------


def file_nodes(store: Store, b: int, wanted: set[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """The File and Document nodes of these (repo, path) pairs, through the index on names (a file's name is its
    path)."""
    names = sorted({path for _, path in wanted})
    ids: list[str] = []
    for start in range(0, len(names), 500):
        chunk = names[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        ids += [
            row[0]
            for row in store.db.execute(
                f"SELECT node_id FROM nodes WHERE name IN ({marks}) AND kind IN ('File', 'Document')"
                " AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)",
                (*chunk, b, b),
            )
        ]
    out = {}
    for node in store.nodes(ids, b).values():
        key = (node["props"].get("repo"), node["props"].get("path"))
        if key in wanted:
            out[key] = node
    return out


def symbols_of(store: Store, b: int, file_id: str) -> list[dict]:
    """The Symbol nodes a file defines, through the index on ids: ``symbol:<source>:<path>::<qualname>``."""
    source, sep, path = file_id.partition(":file:")
    if not sep:
        return []
    prefix = f"symbol:{source}:{path}::"
    ids = [
        row[0]
        for row in store.db.execute(
            "SELECT node_id FROM nodes WHERE node_id >= ? AND node_id < ? AND tx_from <= ? AND (tx_to IS NULL OR tx_to > ?)",
            (prefix, prefix + "\U0010ffff", b, b),
        )
    ]
    return [n for n in store.nodes(ids, b).values() if n["kind"] == "Symbol" and n["props"].get("file") == file_id]


def extents(store: Store, b: int, file_id: str) -> list[tuple[int, int]]:
    """The line spans of a file's top-level definitions."""
    out = []
    for (span,) in store.db.execute(
        f"SELECT span FROM units WHERE item_id = :item AND {LIVE}", {"item": file_id, "b": b}
    ):
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


# -- from the result to the nodes ------------------------------------------------------------------


def parse_output(output: Output, paths: Paths) -> tuple[list[Line], set[tuple[str, str]]]:
    """The parsed lines of a result, and the first MAX_FILES (repo, path) pairs they and the path arguments may name."""
    parsed = [line for line in (parse_line(t, output.numbered, output.count) for t in output.lines) if line]
    wanted: dict[tuple[str, str], None] = {}
    for raw in (*output.paths, *(line.path or line.whole for line in parsed)):
        key = paths(raw)
        if key is not None and len(wanted) < MAX_FILES:
            wanted.setdefault(key)
    return parsed, set(wanted)


def matched_lines(output: Output, parsed: list[Line], paths: Paths, files: dict) -> list[tuple]:
    """(repo, path), line number (None: the file only) and matched text of each matched line, in the result's order.
    A path the graph has no file for is no path: ``12:text`` under a heading names line 12 of the heading's file,
    and every line of a search in one file belongs to that file."""
    single = [paths(p) for p in output.paths]
    current = single[0] if len(single) == 1 and single[0] in files else None
    hits = []
    for line in parsed:
        named, whole = paths(line.path), None if line.path else paths(line.whole)
        if named in files:
            hits.append((named, line.number, line.text))
        elif whole in files:
            current = whole
            hits.append((whole, None, ""))
        elif line.bare is not None and current is not None:
            hits.append((current, None if output.count else line.bare, line.bare_text))
    return hits


def short_name(symbol_id: str) -> str:
    """The last part of a symbol's qualified name: ``search`` for ``symbol:r:p.py::Store.search``."""
    return symbol_id.rpartition("::")[2].rpartition(".")[2]


class FileSymbols:
    """The symbols of one file and where they start, as the graph read it. The checkout may differ from the commit
    the graph read, so a line number alone never places a match: a line that defines a name goes to the symbol of
    that name, and any other line to the symbol around its number only when that symbol calls a name the line
    holds. When a definition sits elsewhere than the graph has it, the file has moved, and no other line of it is
    placed by number."""

    def __init__(self, store: Store, b: int, file_id: str):
        self.store, self.b = store, b
        self.symbols = [s for s in symbols_of(store, b, file_id) if isinstance(s["props"].get("line"), int)]
        self.spans = extents(store, b, file_id)
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
            edges = self.store.edges_of(symbol["id"], self.b, "out", limit=MAX_EDGES)
            self.callees[symbol["id"]] = {short_name(e["dst"]) for e in edges if e["rel"] == "calls"}
        return (symbol, False) if names & self.callees[symbol["id"]] else (None, False)


def find_matches(store: Store, b: int, hits, files: dict, visible) -> list[Match]:
    """The visible nodes the hits fall in, best first: symbols whose definition matched, other symbols, then files."""
    per_file: dict[str, FileSymbols] = {}
    placed = []
    for order, (key, number, text) in enumerate(hits):
        file = files.get(key)
        if file is None or not visible(file["label"]):
            continue
        symbols = None
        if number is not None and file["kind"] == "File":
            if file["id"] not in per_file:
                per_file[file["id"]] = FileSymbols(store, b, file["id"])
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
    """Reads the edges into matched nodes once per node, under the sink's clearance."""

    def __init__(self, store: Store, b: int, ceiling: tuple[int, int], deadline: float):
        self.store, self.b, self.ceiling, self.deadline = store, b, ceiling, deadline
        self.cache: dict[tuple[str, tuple], list[dict]] = {}

    def visible(self, label) -> bool:
        return label[0] <= self.ceiling[0] and label[1] <= self.ceiling[1]

    def into(self, node_id: str, rels: tuple) -> list[dict]:
        """The visible, live nodes with an edge of ``rels`` into ``node_id``, by id."""
        key = (node_id, rels)
        if key not in self.cache:
            _check(self.deadline)
            edges = self.store.edges_into(node_id, self.b, rels, STATUSES, self.ceiling, limit=MAX_EDGES)
            sources = self.store.nodes({e["src"] for e in edges if e["src"] != node_id}, self.b)
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


def find_lines(project, output: Output, deadline: float, sink: str = SINK) -> list[str]:
    """The lines the hook adds for one result, at most MAX_NODES."""
    try:
        clearance = project.policy.clearance(sink)
    except KeyError:
        return []  # the sink is not declared: it sees nothing
    store = Store.open_readonly(project)
    if store is None:
        return []
    try:
        # SQLite calls this every thousand VM steps; a non-zero answer aborts the running query.
        store.db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        b = store.latest_ready()
        if b is None:
            return []
        return _lines_of(store, b, (clearance.level, clearance.location), project, output, deadline)
    finally:
        store.close()


def _lines_of(store: Store, b: int, ceiling, project, output: Output, deadline: float) -> list[str]:
    paths = Paths(repo_roots(project), output.base)
    parsed, wanted = parse_output(output, paths)
    if not wanted:
        return []
    files = file_nodes(store, b, wanted)
    hits = matched_lines(output, parsed, paths, files)
    _check(deadline)
    links = Links(store, b, ceiling, deadline)
    lines: list[str] = []
    shown_files: set[str] = set()
    for match in find_matches(store, b, hits, files, links.visible):
        if len(lines) >= MAX_NODES:
            break
        if match.node is match.file and match.file["id"] in shown_files:
            continue  # a symbol of this file already names its importers' tests and steps
        line = render_line(match, *links.of(match))
        if line:
            lines.append(line)
            shown_files.add(match.file["id"])
    return lines


def post_search(stdin_text: str, only: str | None = None, sink: str = SINK, env=None) -> str | None:
    """The additionalContext for one PostToolUse event, or None to add nothing. Raises Timeout past BUDGET and any
    other unexpected error; an unbound directory, a missing store or a result with no match return None."""
    deadline = time.monotonic() + BUDGET
    if not enabled(env):
        return None
    try:
        event = json.loads(stdin_text)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("hook_event_name", "PostToolUse") != "PostToolUse":
        return None
    output = read_output(event, only)
    if output is None or not output.lines:
        return None
    try:
        project = resolve_project(None)
    except ProjectError:
        return None
    lines = find_lines(project, output, deadline, sink)
    return "\n".join([NOTE, *lines]) if lines else None
