"""What an ``rg`` or ``grep`` command in a shell line searches, as far as its output depends on it: the directory it
runs in after any ``cd`` before it, its path arguments, and the options that change what a line of its output is.

Only the first ``rg`` or ``grep`` that starts a pipeline counts: one further down filters another command's output,
so its lines name no file of the project. Options that make the output something other than matches (``-L``, ``-v``,
``--json``, ``-Z``, ``--files``) leave nothing to read.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field

SEARCH_COMMANDS = ("rg", "grep")
MAX_COMMAND = 4000  # characters of a shell line worth parsing
MSYS_DRIVE = re.compile(r"/([A-Za-z])(?=/|$)")
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
    "--file",
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
    "--regexp",
    "--replace",
    "--sort",
    "--sortr",
    "--threads",
    "--type",
    "--type-add",
    "--type-not",
}
# Options after which the output holds no match of a file: files without one, lines that do not match, JSON events,
# names ended by NUL, file or type lists, nothing at all.
NO_MATCHES_LONG = {
    "--files",
    "--files-without-match",
    "--invert-match",
    "--json",
    "--null",
    "--null-data",
    "--type-list",
    "--quiet",
}
NO_MATCHES_SHORT = {"rg": set("vLq0"), "grep": set("vLZzq")}
PATTERN_LONG = {"--regexp", "--file"}  # the patterns come from these, so the first argument is a path
PATTERN_SHORT = "ef"
NUMBERED_LONG = {"--line-number", "--vimgrep"}
NUMBERED_SHORT = {"rg": set("np"), "grep": set("n")}
COUNT_LONG = {"--count", "--count-matches"}


@dataclass
class GrepCall:
    """One rg or grep at the head of a pipeline: ``base`` is where it runs (None when unknown), ``paths`` its path
    arguments as written, ``numbered`` whether its lines carry line numbers, ``count`` whether they are counts."""

    command: str
    base: str | None
    paths: list[str] = field(default_factory=list)
    numbered: bool = False
    count: bool = False


def native(path: str) -> str:
    """A path as this system names it: ``~`` expanded, and on Windows an MSYS drive (/c/x) as C:/x."""
    if os.name == "nt":
        found = MSYS_DRIVE.match(path)
        if found:
            return f"{found.group(1).upper()}:{path[2:] or '/'}"
    return os.path.expanduser(path)


def parse(command, base: str | None, only: str | None = None) -> GrepCall | None:
    """The first rg or grep that starts a pipeline in ``command``, run from ``base`` after any ``cd`` before it; None
    when there is none, when ``only`` names the other command, or when its output holds no matches to read."""
    if not isinstance(command, str) or len(command) > MAX_COMMAND:
        return None
    try:
        lexer = shlex.shlex(command.replace("\\\n", " ").replace("\n", " ; "), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    for argv in pipeline_heads(tokens):
        while argv and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
            argv = argv[1:]  # FOO=bar rg ...
        name = os.path.basename(argv[0]) if argv else ""
        if name in ("cd", "pushd"):
            base = chdir(base, argv[1:])
        elif name in SEARCH_COMMANDS:
            if only and name != only:
                return None  # the other handler answers for this command
            return _options(name, argv[1:], base)
    return None


def chdir(base: str | None, args: list[str]) -> str | None:
    target = args[0] if args else "~"
    if target == "-" or "$" in target or "`" in target:
        return None  # somewhere the hook cannot know
    target = native(target)
    if os.path.isabs(target):
        return os.path.normpath(target)
    return os.path.normpath(os.path.join(base, target)) if base else None


def pipeline_heads(tokens: list[str]) -> list[list[str]]:
    """The argv of each command that starts a pipeline, in order, redirections left out."""
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


def _long(call: GrepCall, arg: str, args: list[str], i: int) -> int | None:
    """Read one long option at ``args[i - 1]``; the index of the next argument, or None when the output holds no
    matches."""
    key, eq, _ = arg.partition("=")
    if key in NO_MATCHES_LONG:
        return None
    call.numbered = call.numbered or key in NUMBERED_LONG
    call.count = call.count or key in COUNT_LONG
    if key == "--no-line-number":
        call.numbered = False
    return i + 1 if key in VALUE_LONG and not eq and i < len(args) else i


def _short(call: GrepCall, cluster: str, args: list[str], i: int) -> tuple[int | None, bool]:
    """Read a cluster of short options (``-rn``); the index of the next argument (None when the output holds no
    matches) and whether the cluster gave the patterns (``-e``, ``-f``)."""
    name = call.command
    for j, ch in enumerate(cluster):
        if ch in NO_MATCHES_SHORT[name]:
            return None, False
        call.numbered = (call.numbered or ch in NUMBERED_SHORT[name]) and ch != "N"
        call.count = call.count or ch == "c"
        if ch in VALUE_SHORT[name]:
            if not cluster[j + 1 :] and i < len(args):
                i += 1
            return i, ch in PATTERN_SHORT
    return i, False


def _options(name: str, args: list[str], base: str | None) -> GrepCall | None:
    call = GrepCall(name, base)
    positional, explicit, options_done, i = [], False, False, 0
    while i < len(args):
        arg = args[i]
        i += 1
        if options_done or arg == "-" or not arg.startswith("-"):
            positional.append(arg)
        elif arg == "--":
            options_done = True
        elif arg.startswith("--"):
            explicit = explicit or arg.partition("=")[0] in PATTERN_LONG
            next_i = _long(call, arg, args, i)
            if next_i is None:
                return None
            i = next_i
        else:
            next_i, pattern = _short(call, arg[1:], args, i)
            if next_i is None:
                return None
            i, explicit = next_i, explicit or pattern
    call.paths = positional if explicit else positional[1:]
    return call
