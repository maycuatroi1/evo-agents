"""``evo-agents kg hook pre-search``: before Grep, Glob, or an rg or grep command in Bash, point the agent at
graph nodes named like what it searches for, so it can ask kg_context instead.

The note reaches the model as a system reminder, with more authority than a tool result, so it carries only
node ids, kinds and parser-derived paths: never a name, heading or any other text from a source (integrity U).
A wrong hint is noise, so the hook stays quiet unless it is sure. Regex-heavy and short patterns are skipped,
and so are patterns shaped like a YAML key or a list item, which look for structure in a file rather than for
a name. A node must be named like the search rather than merely mention it. Where the search looks (the path
arguments, after the cwd and any ``cd`` before the command) is the strongest signal of what it is after: a
node inside that scope comes first, and a node outside it is named only when an identifier names it exactly,
since a search for that name elsewhere looks for its references. A single plain word must name an entity
exactly, never a heading in another document. A search that matches more than a handful of nodes equally
well gets no hint. Anything unexpected, a missing store or a lookup past the deadline also prints nothing.
"""

from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import shlex
import time
from collections import Counter
from dataclasses import dataclass, field

from evo_agents.kg.project import resolve_project
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store

SINK = "claude-code@anthropic"
BUDGET = 1.5  # seconds for the whole lookup; past it the hook says nothing
MAX_HINTS = 5
MAX_ALTERNATIVES = 3  # a pattern with more alternatives fishes for lines in a file; it names nothing
# Kinds worth a hint, in the order hints are listed: parents before the children they stand for.
KINDS = ("Repo", "Plan", "Seam", "Requirement", "UseCase", "Directory", "Document", "Section", "PlanStep")
# Kinds a hint may name outside the searched scope, when an identifier names the node exactly: searching
# other files for a node's name looks for its references, which kg_context lists. A directory is left out:
# its name is code layout (src/kb_service), rarely the thing a search elsewhere is after.
NAMED_KINDS = ("Repo", "Plan", "Seam", "Requirement", "UseCase", "Document")
# Kinds a single plain word may name: entities that go by an identifier, never a heading or a file name.
WORD_KINDS = ("Repo", "Plan", "Seam", "Requirement", "UseCase")
PATH_KINDS = ("Directory", "Document", "File")
STATUSES = ("declared", "parsed")
SAFE = re.compile(r"[A-Za-z0-9._:/#@-]{1,160}")  # what an id or a path may look like to be printed at all
# Requirement, use case and section codes: KB-01, P1, REQ_7, UC-1.10.6.
CODE = re.compile(r"[A-Za-z]{1,8}[-_]?\d{1,6}(?:[.-]\d{1,6}){0,3}")
IDENT = re.compile(r"[^\W_][-_./:@#][^\W_]|[a-z][A-Z]")  # a separator inside a name, or camelCase
# A YAML key or list item ("evidence:", "- decision:", "  - id: 4", "level: must"): structure, not a name.
YAML_SHAPED = re.compile(r"\s*(?:-\s|[\w.-]+:(?:\s|$))|.*:\s*$")
SHORT_ANCHOR = re.compile(r"[A-Za-z0-9.]+(?:[-_][A-Za-z0-9.]+){0,3}")  # a heading slug of at most four words
OFF = ("0", "false", "off", "no")
NOTE = "evo-kg: the project graph has nodes named like this search; kg_context with these ids gives their links:"

SEARCH_COMMANDS = ("rg", "grep")
# Short options that take a value, per command: the value is attached (-tpy) or the next argument (-t py).
VALUE_SHORT = {"rg": set("ABCEMTdefgjmrt"), "grep": set("ABCDdefm")}
VALUE_LONG = {
    "--after-context",
    "--before-context",
    "--binary-files",
    "--context",
    "--context-separator",
    "--devices",
    "--dfa-size-limit",
    "--directories",
    "--encoding",
    "--engine",
    "--exclude",
    "--exclude-dir",
    "--field-context-separator",
    "--field-match-separator",
    "--glob",
    "--iglob",
    "--ignore-file",
    "--include",
    "--label",
    "--max-columns",
    "--max-count",
    "--max-depth",
    "--max-filesize",
    "--path-separator",
    "--pre",
    "--pre-glob",
    "--regex-size-limit",
    "--replace",
    "--sort",
    "--sortr",
    "--threads",
    "--type",
    "--type-add",
    "--type-not",
}


class Timeout(Exception):
    pass


@dataclass
class Search:
    """What a tool call searches for and where: the literal terms, the directory it runs in, its path
    arguments as written, and file-name filters (``--include``, ``--glob``; a leading ``!`` excludes)."""

    terms: list[str] = field(default_factory=list)
    base: str | None = None
    paths: list[str] = field(default_factory=list)
    globs: list[str] = field(default_factory=list)


def enabled(env=None) -> bool:
    value = (os.environ if env is None else env).get("EVO_KG_GREP_HINTS", "")
    return value.strip().lower() not in OFF


def words(text: str) -> tuple[str, ...]:
    """Lowercase runs of letters and digits, in any script: "KB-01", "kb_01" and "KB 01" all read the same."""
    return tuple(re.findall(r"[^\W_]+", text.lower()))


# -- what the tool call searches for ------------------------------------------------------------------


def search_terms(event: dict, only: str | None = None) -> list[str]:
    """The literal strings a PreToolUse event searches for, keeping only those worth a hint. ``only`` names
    the Bash command (rg or grep) this handler answers for, so a pipeline matching both answers once."""
    search = parse_search(event, only)
    return search.terms if search else []


def parse_search(event: dict, only: str | None = None) -> Search | None:
    """The terms worth a hint and the scope of a PreToolUse event, or None when it searches for nothing."""
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    cwd = event.get("cwd")
    base = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path, glob = tool_input.get("path"), tool_input.get("glob")
    if tool == "Grep":
        search = Search(regex_literals(tool_input.get("pattern")), base)
        search.paths = [path] if isinstance(path, str) and path else []
        search.globs = [glob] if isinstance(glob, str) and glob else []
    elif tool == "Glob":
        search = Search(glob_literals(tool_input.get("pattern")), base)
        search.paths = [path] if isinstance(path, str) and path else []
    elif tool == "Bash":
        search = command_search(tool_input.get("command"), only, base)
    else:
        return None
    if search is None:
        return None
    search.terms = [t for t in (clean(term) for term in search.terms) if t and worth(t)]
    return search if search.terms else None


def clean(term: str) -> str | None:
    """A term without the markdown around it ("## Known gaps", "| Redeploy |"), or None when it is shaped
    like a YAML key or list item: such a search looks for structure in a file, not for a name."""
    if YAML_SHAPED.match(term):
        return None
    return term.strip().lstrip("#").strip().strip("|").strip() or None


def worth(term: str) -> bool:
    """A term names something when one of its words has three characters or more, or it reads as a code
    such as KB-01 or P1."""
    found = words(term)
    if len(term) > 120 or not 0 < len(found) <= 8:
        return False
    return any(len(w) >= 3 for w in found) or CODE.fullmatch(term.strip()) is not None


def regex_literals(pattern, dialect: str = "ere") -> list[str]:
    """A regex as the literal strings it matches, when it is plain text with escapes, anchors, word boundaries
    and at most MAX_ALTERNATIVES alternatives; nothing when it uses classes, groups, repetition or wildcards.
    In a basic regex (grep without -E), ``\\|`` separates alternatives and ``|``, ``(`` or ``+`` are plain
    characters; in an extended one (grep -E, rg, the Grep tool) it is the other way round."""
    if not isinstance(pattern, str) or len(pattern) > 200:
        return []
    basic = dialect == "bre"
    alternatives, current, i = [], [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if i + 1 == len(pattern):
                return []
            nxt = pattern[i + 1]
            if nxt in "bBAzZ<>":
                pass  # word boundaries and anchors match no text
            elif basic and nxt == "|":
                alternatives.append("".join(current))
                current = []
            elif basic and nxt in "(){}+?":
                return []  # groups and repetition in a basic regex
            elif nxt.isalnum():
                return []  # \w, \d, \s, \p{..}, \x41: a class, not a name
            else:
                current.append(nxt)
            i += 2
            continue
        if c in "^$":
            pass
        elif c == "|" and not basic:
            alternatives.append("".join(current))
            current = []
        elif c == "." and pattern[i + 1 : i + 2] in ("*", "+", "?", "{"):
            return []
        elif c in "*[]" or (not basic and c in "+?(){}"):
            return []
        else:
            current.append(c)  # an unescaped "." is read as the dot it nearly always means in a name
        i += 1
    alternatives.append("".join(current))
    if len(alternatives) > MAX_ALTERNATIVES:
        return []
    return [a for a in alternatives if a.strip()]


def glob_literals(pattern) -> list[str]:
    """A glob as the name it looks for: the whole path when only a leading **/ is wild, else the last segment
    stripped of wildcards and extension ("**/kg-prototype*.yaml" looks for kg-prototype). "*.py" names nothing."""
    if not isinstance(pattern, str) or len(pattern) > 200 or any(c in pattern for c in "[]{}"):
        return []
    parts = [p for p in pattern.split("/") if p not in ("", ".", "**")]
    if not parts:
        return []
    if not any(c in pattern.replace("**/", "") for c in "*?"):
        return ["/".join(parts)]
    last = parts[-1]
    if not any(c in last for c in "*?"):
        return [last]
    stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", last)
    stem = stem.replace("*", " ").replace("?", " ").strip()
    return [stem] if stem else []


def command_literals(command, only: str | None = None) -> list[str]:
    """The patterns of the first rg or grep that starts a pipeline in a shell command."""
    search = command_search(command, only, None)
    return search.terms if search else []


def command_search(command, only: str | None, base: str | None) -> Search | None:
    """The first rg or grep that starts a pipeline in a shell command, with the directory it runs in after
    any ``cd`` before it. A grep further down a pipeline filters another command's output and is no search
    of the project."""
    if not isinstance(command, str) or len(command) > 4000:
        return None
    try:
        lexer = shlex.shlex(command.replace("\\\n", " ").replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    for argv in _pipeline_heads(tokens):
        while argv and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]  # FOO=bar rg ...
        name = os.path.basename(argv[0]) if argv else ""
        if name in ("cd", "pushd"):
            base = _chdir(base, argv[1:])
        elif name in SEARCH_COMMANDS:
            if only and name != only:
                return None  # the other handler answers for this command
            return _command_search(name, argv[1:], base)
    return None


def _chdir(base: str | None, args: list[str]) -> str | None:
    target = args[0] if args else "~"
    if target == "-" or "$" in target or "`" in target:
        return None  # somewhere the hook cannot know
    target = os.path.expanduser(target)
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(base, target)) if base else None


def _pipeline_heads(tokens: list[str]) -> list[list[str]]:
    heads, current, stage, skip = [], [], 0, False
    tokens = [*tokens, ";"]
    for k, tok in enumerate(tokens):
        if skip:
            skip = False
            continue
        if tok.isdigit() and tokens[k + 1][:1] in ("<", ">"):
            continue  # the file descriptor of 2>/dev/null
        if tok and set(tok) <= set("();<>|&"):
            if "<" in tok or ">" in tok:
                skip = True  # a redirection: its target is not an argument
                continue
            if stage == 0 and current:
                heads.append(current)
            current = []
            stage = stage + 1 if tok in ("|", "|&") else 0
            continue
        current.append(tok)
    return heads


def _command_search(name: str, args: list[str], base: str | None) -> Search | None:
    explicit, positional, globs, i = [], [], [], 0
    fixed, dialect, options_done = False, "ere" if name == "rg" else "bre", False
    while i < len(args):
        arg = args[i]
        i += 1
        if options_done or arg == "-" or not arg.startswith("-"):
            positional.append(arg)
            continue
        if arg == "--":
            options_done = True
            continue
        if arg.startswith("--"):
            key, eq, value = arg.partition("=")
            if key in ("--files", "--file", "--type-list"):
                return None  # lists files, or reads its patterns from a file
            if key in VALUE_LONG | {"--regexp"} and not eq and i < len(args):
                value, i = args[i], i + 1
            if key == "--fixed-strings":
                fixed = True
            elif key in ("--extended-regexp", "--perl-regexp"):
                dialect = "ere"
            elif key == "--basic-regexp":
                dialect = "bre"
            elif key == "--regexp":
                explicit.append(value)
            elif key in ("--include", "--glob", "--iglob"):
                globs.append(value)
            elif key == "--exclude":
                globs.append("!" + value)
            continue
        cluster = arg[1:]
        for j, ch in enumerate(cluster):
            if ch == "F":
                fixed = True
            elif name == "grep" and ch in "EP":
                dialect = "ere"
            elif name == "grep" and ch == "G":
                dialect = "bre"
            if ch in VALUE_SHORT[name]:
                value = cluster[j + 1 :]
                if not value and i < len(args):
                    value, i = args[i], i + 1
                if ch == "f":
                    return None
                if ch == "e":
                    explicit.append(value)
                if ch == "g" and name == "rg":
                    globs.append(value)
                break
    patterns = explicit or positional[:1]
    terms: list[str] = []
    for pattern in patterns:
        terms.extend([pattern] if fixed else regex_literals(pattern, dialect))
    paths = positional if explicit else positional[1:]
    return Search([t for t in terms if t.strip()][:MAX_ALTERNATIVES], base, paths, globs)


# -- where it searches --------------------------------------------------------------------------------


def _real(path: str) -> str:
    return os.path.realpath(os.path.normpath(path))


def scope_of(project, search: Search) -> list[tuple[str, str]]:
    """(repo, path inside it) for each place the search looks, from its path arguments or else the
    directory it runs in; the path is "" for a whole repo and may be a glob. Places outside every repo of
    the project, or that the hook cannot resolve, add nothing."""
    harness = project.harness
    roots = [(harness.root.name, _real(str(harness.root)))]
    for repo in harness.repos():
        path = harness.repo_path(repo) if isinstance(repo, dict) and isinstance(repo.get("name"), str) else None
        if path is not None:
            roots.append((repo["name"], _real(str(path))))
    out: list[tuple[str, str]] = []
    for raw in search.paths or [""]:
        if "$" in raw or "`" in raw:
            continue
        raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            if not search.base:
                continue
            raw = os.path.join(search.base, raw)
        target = _real(raw)
        for name, root in roots:
            if target == root:
                out.append((name, ""))
            elif target.startswith(root + os.sep):
                out.append((name, os.path.relpath(target, root).replace(os.sep, "/")))
            elif root.startswith(target + os.sep):
                out.append((name, ""))  # an ancestor of the repo, such as the workspace
    return out


def _glob(pattern: str) -> re.Pattern:
    """A shell glob over a relative path: * and ? stay inside one segment, ** crosses them."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        else:
            out.append({"*": "[^/]*", "?": "[^/]"}.get(pattern[i], re.escape(pattern[i])))
            i += 1
    return re.compile("".join(out))


def _expand(glob: str) -> list[str]:
    match = re.search(r"\{([^{}]*)\}", glob)
    if not match:
        return [glob]
    return [glob[: match.start()] + alt + glob[match.end() :] for alt in match.group(1).split(",")]


def _filtered(path: str, globs: list[str]) -> bool:
    """Whether the file-name filters of the search let this path through."""
    name = posixpath.basename(path)

    def hit(glob: str) -> bool:
        return any(
            _glob(g.lstrip("/")).fullmatch(path) if "/" in g else fnmatch.fnmatch(name, g) for g in _expand(glob)
        )

    wanted = [g for g in globs if not g.startswith("!")]
    if wanted and not any(hit(g) for g in wanted):
        return False
    return not any(hit(g[1:]) for g in globs if g.startswith("!"))


def _globbed(path: str, spath: str) -> bool:
    """Whether a glob path argument (plans/*.yaml, src/app*) names this path or a directory above it."""
    if not any(c in spath for c in "*?"):
        return False
    glob, parts = _glob(spath), path.split("/")
    return any(glob.fullmatch("/".join(parts[:k])) for k in range(1, len(parts) + 1))


def _inside(locations, scope, globs) -> tuple[bool, bool]:
    """(some location lies inside the scope, some location is exactly a file the search names)."""
    inside = exact = False
    for repo, path in locations:
        for srepo, spath in scope:
            if srepo != repo:
                continue
            if path is None:  # the repo itself: inside when the search covers all of it
                inside = inside or spath == ""
                continue
            if globs and not _filtered(path, globs):
                continue
            if spath == "" or path == spath or path.startswith(spath + "/") or _globbed(path, spath):
                inside = True
                exact = exact or path == spath
    return inside, exact


# -- which nodes are named like it --------------------------------------------------------------------


def _shape(term: str) -> str:
    """The shape of a term: "word" for a single plain word, "ident" for an identifier (kb-use-cases,
    plan:demo, KB-01, docs/guide.md), "phrase" for words separated by spaces."""
    stripped = term.strip()
    if CODE.fullmatch(stripped):
        return "ident"
    if len(words(stripped)) == 1:
        return "word"
    return "ident" if IDENT.search(stripped) and not re.search(r"\s", stripped) else "phrase"


def _forms(node: dict) -> tuple[list, list]:
    """(names a term may equal, names a term may sit inside). A section goes by its heading and anchor, a
    file or directory by its path, file name and stem, anything else by its name and id."""
    kind, props = node["kind"], node["props"]
    aliases = [a for a in node["aliases"] if isinstance(a, str)]
    if kind == "Section":
        names = [node["name"], props.get("anchor")]
        return names + aliases, names
    if kind in PATH_KINDS:
        path = str(props.get("path") or node["name"] or "").rstrip("/")
        base = posixpath.basename(path)
        stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", base)
        return [path, node["name"], base, stem, *aliases], [path, node["name"]]
    local = node["id"].rsplit(":", 1)[-1]
    return [node["name"], node["id"], local, *aliases], [node["name"], local]


def _tier(node: dict, term: tuple[str, ...]) -> int | None:
    """0 when the node is named exactly like the term, 1 when a name contains the term's words in order
    (only for terms of two words or more), None otherwise. Body text never counts."""
    exact, containing = _forms(node)
    if any(words(n) == term for n in exact if isinstance(n, str) and n):
        return 0
    if len(term) > 1:
        size = len(term)
        for name in containing:
            form = words(name) if isinstance(name, str) else ()
            if any(form[k : k + size] == term for k in range(len(form) - size + 1)):
                return 1
    return None


def _eligible(kind: str, tier: int, shape: str, inside: bool, exact_file: bool) -> bool:
    if kind == "Repo" and tier:
        return False  # a repo only when the term is its name
    if shape == "word":
        return tier == 0 and inside and (kind in WORD_KINDS or (kind == "Section" and exact_file))
    if inside:
        return True
    return tier == 0 and shape == "ident" and kind in NAMED_KINDS


def _locations(store, nodes: dict[str, dict], parents: dict[str, dict], b: int) -> dict[str, list]:
    """(repo, path) of where each node lives: its own path, its document's, or the files it is derived
    from. A repo also stands for itself, as (name, None)."""
    out: dict[str, list] = {}
    derived: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        props = node["props"]
        if node["kind"] == "Section":
            props = (parents.get(props.get("item")) or {}).get("props") or {}
        if isinstance(props.get("repo"), str) and isinstance(props.get("path"), str):
            out[nid] = [(props["repo"], props["path"])]
            continue
        out[nid] = [(node["name"], None)] if node["kind"] == "Repo" else []
        derived[nid] = [
            unit.rpartition("#")[0] for d in store.derivations(nid, b) for unit in d["units"] if isinstance(unit, str)
        ]
    items = store.nodes({i for units in derived.values() for i in units}, b)
    for nid, units in derived.items():
        for item in units:
            props = (items.get(item) or {}).get("props") or {}
            if isinstance(props.get("repo"), str) and isinstance(props.get("path"), str):
                out[nid].append((props["repo"], props["path"]))
    return out


def find_hints(
    project, terms: list[str], deadline: float, sink: str = SINK, search: Search | None = None
) -> list[tuple[str, str, str | None]]:
    """(id, kind, path) of the visible, parsed or declared nodes named like the terms: inside the scope of
    the search before outside it, exact names before partial ones, the best rank only, parents standing for
    their children, nothing when more than MAX_HINTS remain. Without a search, every node counts as inside."""
    session = Session(project, sink)
    if session.error:
        return []
    scope = scope_of(project, search) if search is not None else None
    if scope == []:
        return []  # the search looks outside the project
    store = Store.open_readonly(project)
    if store is None:
        return []
    try:
        # SQLite calls this every thousand VM steps; a non-zero answer aborts the running query.
        store.db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        b = store.latest_ready()
        if b is None:
            return []
        matches: dict[str, list[tuple[str, int, str]]] = {}
        found: dict[str, dict] = {}
        for term in terms:
            ids = store.search(term, b, limit=50)
            _check(deadline)
            target, shape = words(term), _shape(term)
            for nid, node in store.nodes(ids, b).items():
                if node["kind"] not in KINDS or node["status"] not in STATUSES or not session.visible(node["label"]):
                    continue
                tier = _tier(node, target)
                if tier is not None:
                    found[nid] = node
                    matches.setdefault(nid, []).append((term, tier, shape))
        _check(deadline)
        parents = store.nodes({n["props"].get("item") for n in found.values() if n["kind"] == "Section"} - {None}, b)
        _check(deadline)
        locations = _locations(store, found, parents, b) if scope is not None else {}
        _check(deadline)
    finally:
        store.close()

    def usable(node: dict | None) -> bool:
        return (
            node is not None
            and node["kind"] in KINDS
            and node["status"] in STATUSES
            and session.visible(node["label"])
            and SAFE.fullmatch(node["id"]) is not None
        )

    globs = search.globs if search is not None else []
    place = {nid: _inside(locations[nid], scope, globs) if scope is not None else (True, True) for nid in found}
    # An identifier names one node: one that names several of a kind outside the scope (README.md, AGENTS.md,
    # one per repo) is ambiguous there, and names none of them.
    outside = Counter(
        (term, found[nid]["kind"])
        for nid, found_matches in matches.items()
        for term, tier, shape in found_matches
        if not place[nid][0] and _eligible(found[nid]["kind"], tier, shape, False, False)
    )
    hints: dict[str, tuple[tuple[int, int], dict, str | None, list]] = {}
    for nid, node in found.items():
        inside, exact_file = place[nid]
        ranks = [
            (1 - inside, tier)
            for term, tier, shape in matches[nid]
            if _eligible(node["kind"], tier, shape, inside, exact_file) and (inside or outside[term, node["kind"]] == 1)
        ]
        if not ranks:
            continue
        rank = min(ranks)
        path = node["props"].get("path")
        if node["kind"] == "Section":
            parent = parents.get(node["props"].get("item"))
            path = parent["props"].get("path") if parent else None
            if not SHORT_ANCHOR.fullmatch(str(node["props"].get("anchor") or nid.rpartition("#")[2])):
                node = parent  # a long heading slug is free text: name its document instead
        if not usable(node):
            continue
        if node["kind"] == "Repo" or not isinstance(path, str) or not SAFE.fullmatch(path):
            path = None  # a repo's path is a local directory, not a parser-derived path in it
        if node["id"] not in hints or rank < hints[node["id"]][0]:
            hints[node["id"]] = (rank, node, path, locations.get(nid, []))
    if not hints:
        return []
    best = min(r for r, _, _, _ in hints.values())
    chosen = sorted(
        ((node, path, locs) for r, node, path, locs in hints.values() if r == best),
        key=lambda c: (KINDS.index(c[0]["kind"]), c[0]["id"]),
    )
    out: list[tuple[str, str, str | None]] = []
    dirs: list[tuple[str, str]] = []
    for node, path, locs in chosen:
        if any(node["id"].startswith(prev + sep) for prev, _, _ in out for sep in ("/", "#")):
            continue  # a step under a plan, a section under a document: the parent covers it
        if locs and all(any(r == d and p is not None and p.startswith(dp + "/") for d, dp in dirs) for r, p in locs):
            continue  # a file under a directory already named
        out.append((node["id"], node["kind"], path))
        if node["kind"] == "Directory" and isinstance(node["props"].get("path"), str):
            dirs.append((node["props"].get("repo"), node["props"]["path"].rstrip("/")))
    return out if len(out) <= MAX_HINTS else []


def _check(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise Timeout


def render(hints: list[tuple[str, str, str | None]]) -> str:
    lines = [NOTE]
    for nid, kind, path in hints:
        lines.append(f"- {kind} {nid}" + (f" ({path})" if path else ""))
    return "\n".join(lines)


def pre_search(stdin_text: str, only: str | None = None, sink: str = SINK, env=None) -> str | None:
    """The additionalContext for one PreToolUse event, or None to stay silent."""
    deadline = time.monotonic() + BUDGET
    if not enabled(env):
        return None
    try:
        event = json.loads(stdin_text)
        if not isinstance(event, dict):
            return None
        search = parse_search(event, only)
        if search is None:
            return None
        project = resolve_project(None)
        _check(deadline)
        hints = find_hints(project, search.terms, deadline, sink, search)
        _check(deadline)
    except Exception:
        return None  # unbound directory, stale or locked store, timeout, odd input: a hint is never worth an error
    return render(hints) if hints else None
