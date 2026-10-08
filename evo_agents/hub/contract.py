"""``evo-agents hub contract``: the hub's command line as data, and a check of the hub commands written in markdown.

``contract print`` prints ``{"version": 1, "commands": {...}, "openapi": {...}}``, the owner's side of the seam
hub-cli-v1. ``commands`` is generated from the argparse definitions: for every ``evo-agents hub`` command (keyed
``"hub plan step"``) and every command the agent of a run uses (``evo-agents worker step|ask|notify|plan`` of a plan
run, ``evo-agents worker finding|propose`` of a review run, keyed ``"worker step"``, AGENT_COMMANDS), its positional
arguments and options, and for a command with ``--json`` the keys of what it prints, declared next to the flag with
``json_option``. ``openapi`` is the API's OpenAPI document,
the one ``evo-agents hub openapi`` prints, so ``print`` needs the hub-server extra. evo-cli checks the argv it builds
and the keys it reads against this document, and agent-skills the agent's commands its skills name;
tests/hub/golden/cli-contract.json is the copy the tests compare with, so a change to either part shows up as a diff
there.

``contract check FILE...`` reads every ``evo-agents hub ...`` command, and every ``evo-agents worker`` command of
AGENT_COMMANDS, in the code spans and fenced code blocks of markdown files and reports a subcommand or option the
command line does not have, an option missing its value, a literal value outside an option's choices, and more
positional arguments than the command takes. The other ``evo-agents worker`` commands are not in the contract and
are not read. Placeholders pass:
``<name>``, an all-caps word such as ``PLAN``, anything with a shell variable, and ``...`` (any further arguments);
a value written as alternatives, ``a|b`` (``a\\|b`` in a markdown table), passes when each alternative does.
A command ends at a shell operator (``|``, ``&&``, ``;``, ``>``, ``)``) or a comment. Indented code blocks are not
read. ``check`` needs only the core package; ``--contract FILE`` checks against a saved contract instead of the
command line of this installation.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

CONTRACT_VERSION = 1
EXIT_PROBLEMS = 1
EXIT_USAGE = 2

# `evo-agents worker ...` that the agent of a run runs: a plan run's, then a review run's
AGENT_COMMANDS = ("step", "ask", "notify", "plan", "finding", "propose")
HUB_COMMAND = re.compile(rf"(?<![\w./-])evo-agents\s+(hub|worker\s+({'|'.join(AGENT_COMMANDS)}))\b")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
PLACEHOLDER = re.compile(r"<[A-Za-z][^<>\n]*>")
SENTINEL = "\x1fplaceholder\x1f"  # stands for a <placeholder> while the command is split into words
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
METAVAR = re.compile(r"[A-Z][A-Z0-9_]*")
WILDCARDS = frozenset({"...", "…"})
HELP_FLAGS = frozenset({"-h", "--help"})
OPERATOR = re.compile(r"[();<>|&]+")


# Declaring what --json prints


@dataclass(frozen=True)
class JsonOutput:
    """What a command prints with ``--json``.

    ``kind`` is ``object`` (one object with ``keys``), ``array`` (a list of objects with ``keys``) or ``map`` (an
    object whose keys are data, ``keys`` empty). ``schema`` names the OpenAPI component the hub answers with when
    the command prints that answer, and ``added`` the keys the client adds to it; tests check that ``keys`` is
    exactly the component's properties plus ``added``. ``variants`` lists, per option, what is printed instead
    when that option is given.
    """

    kind: str
    keys: tuple[str, ...] = ()
    schema: str | None = None
    added: tuple[str, ...] = ()
    variants: tuple[tuple[str, JsonOutput], ...] = ()

    def to_json(self) -> dict:
        data: dict = {"kind": self.kind, "keys": list(self.keys)}
        if self.schema:
            data["schema"] = self.schema
        if self.variants:
            data["with"] = {option: output.to_json() for option, output in self.variants}
        return data


def returns_object(*keys: str, schema: str | None = None, added: tuple[str, ...] = (), variants=()) -> JsonOutput:
    return JsonOutput("object", keys, schema, added, tuple(variants))


def returns_array(*keys: str, schema: str | None = None) -> JsonOutput:
    return JsonOutput("array", keys, schema)


def returns_map(schema: str | None = None) -> JsonOutput:
    return JsonOutput("map", (), schema)


def json_option(parser: argparse.ArgumentParser, output: JsonOutput, help: str = "machine-readable output") -> None:
    """Add ``--json`` to ``parser`` and declare what it prints."""
    parser.add_argument("--json", action="store_true", help=help)
    parser.set_defaults(json_output=output)


# The command line as data


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _value_type(action: argparse.Action) -> str:
    from evo_agents.hub.cli import _positive

    types = {None: "string", str: "string", int: "integer", float: "number", _positive: "integer"}
    if action.type not in types:
        raise ValueError(f"{'/'.join(action.option_strings) or action.dest}: no contract type for {action.type!r}")
    return types[action.type]


def _choices(action: argparse.Action, entry: dict) -> None:
    if action.choices is None:
        return
    if isinstance(action.choices, range) and action.choices.step == 1:
        entry["minimum"], entry["maximum"] = action.choices.start, action.choices.stop - 1
    else:
        entry["choices"] = list(action.choices)


def _option(action: argparse.Action) -> dict:
    switch = action.nargs == 0
    entry = {
        "flags": list(action.option_strings),
        "value": None if switch else _value_type(action),
        "required": bool(action.required),
        "repeatable": isinstance(action, (argparse._AppendAction, argparse._ExtendAction, argparse._CountAction)),
    }
    _choices(action, entry)
    if not switch and action.default is not None and action.default != []:
        entry["default"] = action.default
    return entry


def _positional(action: argparse.Action) -> dict:
    entry = {
        "name": action.dest,
        "metavar": action.metavar or action.dest.upper(),
        "value": _value_type(action),
        "required": action.nargs not in ("?", "*"),
        "repeatable": action.nargs in ("*", "+"),
    }
    _choices(action, entry)
    return entry


def _command(parser: argparse.ArgumentParser) -> dict:
    positionals, options = [], []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._VersionAction)):
            continue
        (options if action.option_strings else positionals).append(
            _option(action) if action.option_strings else _positional(action)
        )
    output = parser.get_default("json_output")
    return {"positionals": positionals, "options": options, "json": output.to_json() if output else None}


def hub_parsers(parser: argparse.ArgumentParser | None = None) -> dict[str, argparse.ArgumentParser]:
    """Every ``evo-agents hub`` command's parser, keyed by its path (``"hub plan step"``)."""
    from evo_agents.cli import build_parser

    hub = _subparsers(parser or build_parser())["hub"]
    found: dict[str, argparse.ArgumentParser] = {}

    def walk(node: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        children = _subparsers(node)
        if not children:
            found[" ".join(path)] = node
        for name, child in children.items():
            walk(child, (*path, name))

    walk(hub, ("hub",))
    return dict(sorted(found.items()))


def contract_parsers(parser: argparse.ArgumentParser | None = None) -> dict[str, argparse.ArgumentParser]:
    """The parser of every command in the contract: those of ``hub_parsers`` and the agent's ``evo-agents worker``
    commands (AGENT_COMMANDS, keyed ``"worker step"``)."""
    from evo_agents.cli import build_parser

    root = parser or build_parser()
    worker = _subparsers(_subparsers(root)["worker"])
    found = {**hub_parsers(root), **{f"worker {name}": worker[name] for name in AGENT_COMMANDS}}
    return dict(sorted(found.items()))


def commands(parser: argparse.ArgumentParser | None = None) -> dict:
    """The ``commands`` part of the contract: needs only the core package."""
    return {path: _command(node) for path, node in contract_parsers(parser).items()}


def build() -> dict:
    """The whole contract; the OpenAPI part needs the hub-server extra."""
    from evo_agents.hub.openapi import document

    return {"version": CONTRACT_VERSION, "commands": commands(), "openapi": document()}


def render(contract: dict) -> str:
    return json.dumps(contract, indent=2, ensure_ascii=False) + "\n"


# Commands written in markdown


@dataclass(frozen=True)
class Found:
    """One ``evo-agents hub ...`` command of a markdown file."""

    path: str
    line: int
    text: str


def _logical_lines(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join the lines of a code block that end in a backslash with the next one."""
    joined, start, parts = [], None, []
    for number, line in lines:
        if start is None:
            start = number
        if line.rstrip().endswith("\\"):
            parts.append(line.rstrip()[:-1])
            continue
        parts.append(line)
        joined.append((start, " ".join(parts)))
        start, parts = None, []
    if parts:
        joined.append((start, " ".join(parts)))
    return joined


def _in_block(path: str, lines: list[tuple[int, str]]) -> list[Found]:
    found = []
    for number, line in _logical_lines(lines):
        for match in HUB_COMMAND.finditer(line):
            if re.search(r"(^|\s)#", line[: match.start()]):  # inside a shell comment
                continue
            found.append(Found(path, number, line[match.start() :].split("`", 1)[0].strip()))
    return found


def _in_spans(path: str, lines: list[tuple[int, str]]) -> list[Found]:
    found = []
    paragraphs: list[list[tuple[int, str]]] = [[]]
    for number, line in lines:
        if line.strip():
            paragraphs[-1].append((number, line))
        elif paragraphs[-1]:
            paragraphs.append([])
    for paragraph in paragraphs:
        if not paragraph:
            continue
        text = "\n".join(line for _, line in paragraph)
        first = paragraph[0][0]
        for span in CODE_SPAN.finditer(text):
            content = span.group(2)
            for match in HUB_COMMAND.finditer(content):
                offset = span.start(2) + match.start()
                number = first + text.count("\n", 0, offset)
                found.append(Found(path, number, " ".join(content[match.start() :].split())))
    return found


def find_commands(text: str, path: str = "<text>") -> list[Found]:
    """Every ``evo-agents hub ...`` command in the fenced code blocks and code spans of markdown ``text``."""
    prose: list[tuple[int, str]] = []
    block: list[tuple[int, str]] = []
    found: list[Found] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        opening = FENCE.match(line)
        if fence is None:
            if opening:
                fence, block = opening.group(1), []
            else:
                prose.append((number, line))
            continue
        closing = line.strip()
        if opening and closing and set(closing) == {fence[0]} and len(closing) >= len(fence):
            found.extend(_in_block(path, block))
            fence = None
        else:
            block.append((number, line))
    if fence is not None:  # an unclosed fence runs to the end of the file
        found.extend(_in_block(path, block))
    found.extend(_in_spans(path, prose))
    return sorted(found, key=lambda f: f.line)


def _words(text: str) -> list[str]:
    """The words of a command up to its first shell operator, with <placeholders> kept as one word each."""
    lexer = shlex.shlex(PLACEHOLDER.sub(SENTINEL, text), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    words = []
    for word in lexer:
        if OPERATOR.fullmatch(word):
            if words and words[-1].isdigit() and word[0] in "<>":  # 2>/dev/null
                words.pop()
            break
        word = word.lstrip("[").rstrip("]")  # [--json] in a usage line
        if word:
            words.append(word)
    return words


def _placeholder(word: str) -> bool:
    return SENTINEL in word or "$" in word or word in WILDCARDS or bool(METAVAR.fullmatch(word))


def _check_value(what: str, entry: dict, value: str) -> str | None:
    if _placeholder(value):
        return None
    if "|" in value.strip("|"):  # a usage line's alternatives, such as a markdown table's push\|merge: each one
        return next((p for part in value.split("|") if (p := _check_value(what, entry, part))), None)
    choices = entry.get("choices")
    if choices is not None and value not in [str(choice) for choice in choices]:
        return f"{what} {value!r} is not one of {', '.join(str(c) for c in choices)}"
    if "minimum" in entry:
        if not value.lstrip("-").isdigit() or not entry["minimum"] <= int(value) <= entry["maximum"]:
            return f"{what} {value!r} is not a whole number from {entry['minimum']} to {entry['maximum']}"
    elif entry.get("value") in ("integer", "number"):
        try:
            int(value) if entry["value"] == "integer" else float(value)
        except ValueError:
            return f"{what} {value!r} is not {'a whole number' if entry['value'] == 'integer' else 'a number'}"
    return None


def check_words(contract_commands: dict, words: list[str]) -> list[str]:
    """What is wrong with ``evo-agents hub ...`` split into ``words`` (``evo-agents`` first), by the contract."""
    path, rest = words[1:2], words[2:]
    while " ".join(path) not in contract_commands:
        prefix = " ".join(path)
        children = sorted({key.split()[len(path)] for key in contract_commands if key.startswith(prefix + " ")})
        if not children:
            return [f"`evo-agents {prefix}` is not a command"]
        if not rest or rest[0] in HELP_FLAGS or rest[0] in WILDCARDS or SENTINEL in rest[0]:
            return []  # the group itself, as in "see `evo-agents hub plan`"
        word = rest.pop(0)
        if word.startswith("-"):
            return [f"`evo-agents {prefix}` takes a subcommand ({', '.join(children)}), not the option {word}"]
        if word not in children:
            return [f"`evo-agents {prefix}` has no subcommand {word!r}; it has {', '.join(children)}"]
        path.append(word)
    name = " ".join(path)
    spec = contract_commands[name]
    options = {flag: entry for entry in spec["options"] for flag in entry["flags"]}
    problems, given, wildcard, index = [], [], False, 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if word in WILDCARDS:
            wildcard = True
        elif word == "--":
            given.extend(rest[index:])
            break
        elif word.startswith("-") and len(word) > 1 and not word.lstrip("-").isdigit():
            flag, equals, value = word.partition("=")
            if flag in HELP_FLAGS:
                continue
            entry = options.get(flag)
            if entry is None:
                problems.append(f"`evo-agents {name}` has no option {flag}")
                continue
            if entry["value"] is None:
                if equals:
                    problems.append(f"{flag} takes no value")
                continue
            if not equals:
                if index >= len(rest):
                    problems.append(f"{flag} needs a value")
                    break
                value = rest[index]
                index += 1
            problem = _check_value(flag, entry, value)
            if problem:
                problems.append(problem)
        else:
            given.append(word)
    positionals = spec["positionals"]
    if not any(p["repeatable"] for p in positionals) and not wildcard and len(given) > len(positionals):
        extra = " ".join("<...>" if SENTINEL in word else word for word in given[len(positionals) :])
        problems.append(f"`evo-agents {name}` takes at most {len(positionals)} argument(s); {extra!r} is too many")
    for entry, value in zip(positionals, given, strict=False):
        problem = _check_value(entry["metavar"], entry, value)
        if problem:
            problems.append(problem)
    return problems


def check_command(contract_commands: dict, text: str) -> list[str]:
    try:
        words = _words(text)
    except ValueError as exc:
        return [f"cannot be read as a shell command ({exc})"]
    return check_words(contract_commands, words)


def check_files(contract_commands: dict, paths: list[str]) -> tuple[int, list[str]]:
    """How many hub commands ``paths`` hold, and one line per problem."""
    count, lines = 0, []
    for path in paths:
        text = Path(path).read_text(encoding="utf-8")
        for found in find_commands(text, path):
            count += 1
            shown = found.text if len(found.text) <= 100 else found.text[:97] + "..."
            for problem in check_command(contract_commands, found.text):
                lines.append(f"{found.path}:{found.line}: {shown}: {problem}")
    return count, lines


def load_commands(path: str) -> dict:
    """The ``commands`` of a saved contract (``contract print`` output)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    held = data if isinstance(data, dict) else {}
    if held.get("version") != CONTRACT_VERSION or not isinstance(held.get("commands"), dict):
        raise ValueError(f"{path} is not a version {CONTRACT_VERSION} hub contract")
    return held["commands"]


# Commands


def cmd_print(args) -> int:
    from evo_agents.hub.cli import _missing_extra

    try:
        text = render(build())
    except ImportError as exc:
        return _missing_extra(exc, args)
    sys.stdout.write(text)
    return 0


def cmd_check(args) -> int:
    try:
        contract_commands = load_commands(args.contract) if args.contract else commands()
        count, problems = check_files(contract_commands, args.files)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    for line in problems:
        print(line)
    files = f"{len(args.files)} file(s)"
    if problems:
        print(f"{len(problems)} problem(s) in the {count} hub command(s) of {files}")
        return EXIT_PROBLEMS
    print(f"{count} hub command(s) in {files}, all in the contract")
    return 0


def register(hsub) -> None:
    from evo_agents.hub.cli import _server_command

    contract = hsub.add_parser("contract", help="the hub command line as data (seam hub-cli-v1), and a check of docs")
    csub = contract.add_subparsers(dest="contract_command", required=True)
    printed = csub.add_parser(
        "print", help="print the commands, their options and --json keys, and the OpenAPI document, as JSON"
    )
    printed.set_defaults(func=_server_command(cmd_print))
    checked = csub.add_parser(
        "check", help="report `evo-agents hub` commands in markdown code that the command line does not have"
    )
    checked.add_argument("files", metavar="FILE", nargs="+", help="markdown files")
    checked.add_argument("--contract", help="a saved `contract print` output to check against (default: this one)")
    checked.set_defaults(func=cmd_check)
