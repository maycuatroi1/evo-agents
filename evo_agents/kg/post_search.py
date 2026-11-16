"""``evo-agents kg hook post-search``: after a Grep, or an rg or grep command in Bash, add what the project graph
links to the lines it matched: who calls the function or method a matched line falls in, the tests that call it or
import its file, and the plan steps that name it or its file.

The search runs first and its output reaches the model as it is; the hook only adds lines, through
``additionalContext``, and never sets ``updatedToolOutput``. So when the PostToolUse hook of evo-hub cuts a long result
down to its head and tail (``evo_agents.hub.spool``), what this hook adds stays whole, and it reads the result as Claude
Code hands it to every hook, before that cut. The note reaches the model with the authority of a system reminder, so it
carries only node ids that the parser derived from paths and code names, never a name, heading or other text of a
source (integrity U), and only nodes and edges whose label the sink may see.

This module is the hook's side on this machine: it reads the PostToolUse event, parses the grep output and maps its
paths to (repo, path) with the directories of the bound project's harness and repos (``evo_agents.kg.hook_scope``,
kept between runs so the hook imports neither the harness loader nor PyYAML). The hub holds the graph: the matched
lines go to POST /v1/kg/{project}/grep-links (``evo_agents.hub.kg_grep_hook``), which reads them against the
project's latest graph with the sink's clearance and the member's grant (``evo_agents.kg.grep_links``), and its lines
follow NOTE. The hook runs as ``evo-agents kg hook post-search``, which skips the parsers of every other command
(``evo_agents.cli.main``).

Nothing to add, an unbound directory, no sign-in, no hub, an answer past BUDGET, any error, or a command whose output
holds no matches print nothing. The hook reads no source file and no git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from evo_agents.kg import grep_command
from evo_agents.kg.hook_scope import scope_for

SINK = "claude-code@anthropic"
# Seconds for the whole hook: reading the output and one round trip to the hub, past which it says nothing. A fresh
# process that imports the hub client, reads the sign-in and makes one TLS request to the hub took 225 ms at the
# median and 255 ms at most (15 runs on a loaded Linux machine, 2026-11-16); 1 s leaves room for a slower link and a
# busy hub while a grep still feels immediate.
BUDGET = 1.0
MAX_OUTPUT_LINES = 5000  # lines of a result read
MAX_FILES = 100  # files of a result sent (``grep_links.MAX_FILES``)
MAX_TEXT = 400  # characters of a matched line sent (``grep_links.MAX_TEXT``)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PREFIXED = re.compile(r"(.+?):(\d+):")  # path:line:text
FIRST_COLON = re.compile(r"([^:]+):")  # path:text, path:count
DRIVE = re.compile(r"[A-Za-z]:[\\/]")  # C:\ or C:/, which starts an absolute path on Windows
WINDOWS = os.name == "nt"
BARE = re.compile(r"(\d+)(?::|$)")  # line:text of a single file or under a heading; a count alone
OFF = ("0", "false", "off", "no")
NOTE = (
    "evo-kg: what the project graph links to the code this search matched (callers, tests, plan steps); "
    "kg_context or kg_impact with an id gives more:"
)

Ask = Callable[[str, dict, float], list[str]]  # (hub project, request body, deadline) to the note's lines


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


def enabled(env=None) -> bool:
    value = (os.environ if env is None else env).get("EVO_KG_GREP_HINTS", "")
    return value.strip().lower() not in OFF


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
    start = 3 if WINDOWS and DRIVE.match(text) else 0  # the colon of a drive ends no path
    found = FIRST_COLON.match(text, start)
    if found:
        line.path, line.text = text[: found.end(1)], text[found.end() :]
        if count and line.text.strip() == "0":
            return None  # grep -c names files without a match too
    return line


# -- where its paths point -------------------------------------------------------------------------


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


# -- from the result to the request ----------------------------------------------------------------


def parse_output(output: Output, paths: Paths) -> tuple[list[Line], set[tuple[str, str]]]:
    """The parsed lines of a result, and the first MAX_FILES (repo, path) pairs they and the path arguments may name."""
    parsed = [line for line in (parse_line(t, output.numbered, output.count) for t in output.lines) if line]
    wanted: dict[tuple[str, str], None] = {}
    for raw in (*output.paths, *(line.path or (line.whole if line.bare is None else None) for line in parsed)):
        key = paths(raw)
        if key is not None and len(wanted) < MAX_FILES:
            wanted.setdefault(key)
    return parsed, set(wanted)


def matched_lines(output: Output, parsed: list[Line], paths: Paths, files) -> list[tuple]:
    """(repo, path), line number (None: the file only) and matched text of each matched line, in the result's order.
    A path outside ``files`` (the files in the project's repos the result may name) is no path: ``12:text`` under a
    heading names line 12 of the heading's file, and every line of a search in one file belongs to that file. The
    hub leaves out the files its graph lacks."""
    single = [paths(p) for p in output.paths]
    current = single[0] if len(single) == 1 and single[0] in files else None
    hits = []
    for line in parsed:
        named = paths(line.path)
        whole = None if line.path or line.bare is not None else paths(line.whole)  # a number first names no file
        if named in files:
            hits.append((named, line.number, line.text))
        elif whole in files:
            current = whole
            hits.append((whole, None, ""))
        elif line.bare is not None and current is not None:
            hits.append((current, None if output.count else line.bare, line.bare_text))
    return hits


def request(sink: str, hits: list[tuple]) -> dict:
    """The body of POST /v1/kg/{project}/grep-links: the files the hits name, and each hit by its file's index."""
    files: dict[tuple[str, str], int] = {}
    sent = []
    for key, number, text in hits:
        if key not in files:
            files[key] = len(files)
        sent.append({"file": files[key], "line": number, "text": text[:MAX_TEXT]})
    return {"sink": sink, "files": [{"repo": r, "path": p} for r, p in files], "hits": sent}


def post_search(stdin_text: str, ask: Ask, only: str | None = None, sink: str = SINK, env=None) -> str | None:
    """The additionalContext for one PostToolUse event, or None to add nothing: the lines ``ask`` gets from the hub
    for the bound project, before the deadline BUDGET sets. An unbound directory or a result with no match in the
    project's repos return None without asking; ``ask`` raises what it raises."""
    deadline = time.monotonic() + BUDGET
    env = os.environ if env is None else env
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
    scope = scope_for(env)
    if scope is None:
        return None
    paths = Paths(scope.roots, output.base)
    parsed, wanted = parse_output(output, paths)
    hits = matched_lines(output, parsed, paths, wanted)
    if not hits:
        return None
    lines = ask(scope.project, request(sink, hits), deadline)
    return "\n".join([NOTE, *lines]) if lines else None


def add_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--only", choices=["rg", "grep"], help="for Bash, answer only when this command leads (one handler per if)"
    )
    parser.add_argument("--sink", default=SINK, help="sink profile whose clearance applies")
    return parser
