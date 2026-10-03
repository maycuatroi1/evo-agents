"""``evo-agents kg hook pre-search``: before Grep, Glob, or an rg or grep command in Bash, point the agent at
graph nodes named like what it searches for, so it can ask kg_context instead.

The note reaches the model as a system reminder, with more authority than a tool result, so it carries only
node ids, kinds and parser-derived paths: never a name, heading or any other text from a source (integrity U).
A wrong hint is noise, so the hook stays quiet unless it is sure: regex-heavy and short patterns are skipped,
a node must be named like the search rather than merely mention it, and a search that matches more than a
handful of nodes equally well gets no hint. Anything unexpected, a missing store or a lookup past the
deadline also prints nothing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time

from evo_agents.kg.project import resolve_project
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store

SINK = "claude-code@anthropic"
BUDGET = 1.5  # seconds for the whole lookup; past it the hook says nothing
MAX_HINTS = 5
# Kinds worth a hint, in the order hints are listed: parents before the children they stand for.
KINDS = ("Plan", "Seam", "Requirement", "UseCase", "Directory", "Document", "Section", "PlanStep")
STATUSES = ("declared", "parsed")
SAFE = re.compile(r"[A-Za-z0-9._:/#@-]{1,160}")  # what an id or a path may look like to be printed at all
CODE = re.compile(r"[A-Za-z]{1,8}[-_]?\d{1,6}")  # requirement and use case codes: KB-01, P1, REQ_7
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
    tool = event.get("tool_name")
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    if tool == "Grep":
        literals = regex_literals(tool_input.get("pattern"))
    elif tool == "Glob":
        literals = glob_literals(tool_input.get("pattern"))
    elif tool == "Bash":
        literals = command_literals(tool_input.get("command"), only)
    else:
        return []
    return [t for t in literals if worth(t)]


def worth(term: str) -> bool:
    """A term names something when one of its words has three characters or more, or it reads as a code
    such as KB-01 or P1."""
    found = words(term)
    if len(term) > 120 or not 0 < len(found) <= 8:
        return False
    return any(len(w) >= 3 for w in found) or CODE.fullmatch(term.strip()) is not None


def regex_literals(pattern) -> list[str]:
    """A regex as the literal strings it matches, when it is plain text with escapes, anchors, word boundaries
    and at most three alternatives; nothing when it uses classes, groups, repetition or wildcards."""
    if not isinstance(pattern, str) or len(pattern) > 200:
        return []
    alternatives, current, i = [], [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            if i + 1 == len(pattern):
                return []
            nxt = pattern[i + 1]
            if nxt in "bBAzZ<>":
                pass  # word boundaries and anchors match no text
            elif nxt.isalnum():
                return []  # \w, \d, \s, \p{..}, \x41: a class, not a name
            else:
                current.append(nxt)
            i += 2
            continue
        if c in "^$":
            pass
        elif c == "|":
            alternatives.append("".join(current))
            current = []
        elif c == "." and pattern[i + 1 : i + 2] in ("*", "+", "?", "{"):
            return []
        elif c in "*+?()[]{}":
            return []
        else:
            current.append(c)  # an unescaped "." is read as the dot it nearly always means in a name
        i += 1
    alternatives.append("".join(current))
    if len(alternatives) > 3:
        return []
    return [a.strip() for a in alternatives if a.strip()]


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
    """The patterns of the first rg or grep that starts a pipeline in a shell command. A grep further down a
    pipeline filters another command's output and is no search of the project."""
    if not isinstance(command, str) or len(command) > 4000:
        return []
    try:
        lexer = shlex.shlex(command.replace("\\\n", " ").replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    for argv in _pipeline_heads(tokens):
        while argv and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]  # FOO=bar rg ...
        name = os.path.basename(argv[0]) if argv else ""
        if name in SEARCH_COMMANDS:
            if only and name != only:
                return []  # the other handler answers for this command
            return _command_patterns(name, argv[1:])
    return []


def _pipeline_heads(tokens: list[str]) -> list[list[str]]:
    heads, current, stage, skip = [], [], 0, False
    for tok in [*tokens, ";"]:
        if skip:
            skip = False
            continue
        if tok and set(tok) <= set("();<>|&"):
            if "<" in tok or ">" in tok:
                skip = not tok.endswith("&")  # a redirection: its target is not an argument
                continue
            if stage == 0 and current:
                heads.append(current)
            current = []
            stage = stage + 1 if tok in ("|", "|&") else 0
            continue
        current.append(tok)
    return heads


def _command_patterns(name: str, args: list[str]) -> list[str]:
    explicit, positional, fixed, i = [], [], False, 0
    options_done = False
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
                return []  # lists files, or reads its patterns from a file
            if key == "--fixed-strings":
                fixed = True
            elif key == "--regexp":
                if not eq and i < len(args):
                    value, i = args[i], i + 1
                explicit.append(value)
            elif key in VALUE_LONG and not eq:
                i += 1
            continue
        cluster = arg[1:]
        for j, ch in enumerate(cluster):
            if ch == "F":
                fixed = True
            if ch in VALUE_SHORT[name]:
                value = cluster[j + 1 :]
                if not value and i < len(args):
                    value, i = args[i], i + 1
                if ch == "f":
                    return []
                if ch == "e":
                    explicit.append(value)
                break
    patterns = explicit or positional[:1]
    out: list[str] = []
    for pattern in patterns:
        out.extend([pattern.strip()] if fixed else regex_literals(pattern))
    return [p for p in out if p][:3]


# -- which nodes are named like it --------------------------------------------------------------------


def _tier(node: dict, term: tuple[str, ...]) -> int | None:
    """0 when the node is named exactly like the term, 1 when its name contains the term's words in order
    (only for terms of two words or more), None otherwise. Body text never counts."""
    names = [node["name"], node["id"], node["id"].rsplit(":", 1)[-1], node["props"].get("path"), *node["aliases"]]
    forms = [words(n) for n in names if isinstance(n, str) and n]
    if term in forms:
        return 0
    if len(term) > 1:
        size = len(term)
        for form in forms:
            if any(form[k : k + size] == term for k in range(len(form) - size + 1)):
                return 1
    return None


def find_hints(project, terms: list[str], deadline: float, sink: str = SINK) -> list[tuple[str, str, str | None]]:
    """(id, kind, path) of the visible, parsed or declared nodes named like the terms: the best tier only,
    parents standing for their children, nothing when more than MAX_HINTS remain."""
    session = Session(project, sink)
    if session.error:
        return []
    store = Store.open_readonly(project)
    if store is None:
        return []
    try:
        # SQLite calls this every thousand VM steps; a non-zero answer aborts the running query.
        store.db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        b = store.latest_ready()
        if b is None:
            return []
        tiers: dict[str, int] = {}
        found: dict[str, dict] = {}
        for term in terms:
            ids = store.search(term, b, limit=50)
            _check(deadline)
            target = words(term)
            for nid, node in store.nodes(ids, b).items():
                if node["kind"] not in KINDS or node["status"] not in STATUSES or not session.visible(node["label"]):
                    continue
                tier = _tier(node, target)
                if tier is not None and tier < tiers.get(nid, 2):
                    tiers[nid], found[nid] = tier, node
        _check(deadline)
        parents = store.nodes({n["props"].get("item") for n in found.values() if n["kind"] == "Section"} - {None}, b)
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

    hints: dict[str, tuple[int, dict, str | None]] = {}
    for nid, node in found.items():
        path = node["props"].get("path")
        if node["kind"] == "Section":
            parent = parents.get(node["props"].get("item"))
            path = parent["props"].get("path") if parent else None
            if not SHORT_ANCHOR.fullmatch(str(node["props"].get("anchor") or nid.rpartition("#")[2])):
                node = parent  # a long heading slug is free text: name its document instead
        if not usable(node):
            continue
        if not isinstance(path, str) or not SAFE.fullmatch(path):
            path = None
        tier = tiers[nid]
        if node["id"] not in hints or tier < hints[node["id"]][0]:
            hints[node["id"]] = (tier, node, path)
    if not hints:
        return []
    best = min(t for t, _, _ in hints.values())
    chosen = sorted(
        ((node, path) for t, node, path in hints.values() if t == best),
        key=lambda np: (KINDS.index(np[0]["kind"]), np[0]["id"]),
    )
    out: list[tuple[str, str, str | None]] = []
    for node, path in chosen:
        if any(node["id"].startswith(prev + sep) for prev, _, _ in out for sep in ("/", "#")):
            continue  # a step under a plan, a section under a document: the parent covers it
        out.append((node["id"], node["kind"], path))
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
        terms = search_terms(event, only)
        if not terms:
            return None
        project = resolve_project(None)
        _check(deadline)
        hints = find_hints(project, terms, deadline, sink)
        _check(deadline)
    except Exception:
        return None  # unbound directory, stale or locked store, timeout, odd input: a hint is never worth an error
    return render(hints) if hints else None
