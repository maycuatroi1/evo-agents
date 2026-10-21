"""The skills a session loads and the friction after each load, and the kind of each Bash command that failed: what the
digest of a session (``evo_agents.hub.digest``) holds so the Curator can tell, for each skill and each program, whether
it gets called, whether it snags, and, with the runs (``evo_agents.hub.server.collect``), whether the work gets done.

A Claude Code session loads a skill through the Skill tool (its input's ``skill``) or when the person types ``/name``,
which the transcript holds as ``<command-name>/name</command-name>`` in a message of the person, with no Skill tool
call and no PreToolUse hook. The commands built into Claude Code are written the same way (``/compact``, ``/clear``,
``/model``: BUILTIN_COMMANDS) and are not skills. A Codex session loads one when the person names it (``$name``): the
rollout then holds a message of the person that starts with ``<skill>`` and names it in ``<name>``. A skill the Codex
model reads on its own through the shell leaves no such sign, and is not counted.

Each load opens a window that the next turn of the person closes (``SkillTally``). In it the tally counts the Bash
commands, the ones that failed by kind, and the times the person interrupted the agent (Claude Code's ``[Request
interrupted by user``, Codex's ``turn_aborted``); the turn that closes it counts as a correction when it reads as one
(``review.is_correction``). A skill loaded again while its window is open adds a load, not a second window.

A failed Bash command is of one of KINDS (``error_kind``): ``usage`` when the program was called wrong, ``environment``
when its environment failed it (the hub or the network, a timeout, a command the harness blocked:
``review.environment_cause``), ``other`` otherwise. For evo-agents the exit status says it (EXIT_KINDS, docs/hub.md),
from the first line of the result Claude Code writes, ``Exit code N``, or from the exit code Codex records, when the
command ran evo-agents alone; anything else is read from the output (USAGE, ``usage:``, ``unrecognized arguments``,
``invalid choice``). A failure counts for a program (``blame``) when the command ran that program alone, or when a
line of the output starts with the program's name and a colon, as argparse and most tools write their errors; the
failure of another command that runs several programs counts for none of them (``BashTally.compound``), since its exit
status is its last part's and its output may be any part's. Standard library only: the Stop hook runs on a core
install.
"""

from __future__ import annotations

import os
import re
import shlex
from collections import Counter, defaultdict

from evo_agents.hub.client import EXIT_TEMPFAIL, EXIT_UNAVAILABLE, EXIT_USAGE
from evo_agents.hub.review import environment_cause, is_correction

MAX_NAME = 200  # characters of a tool, program, skill or model name
MAX_SKILLS = 100  # skills one digest lists, the most loaded first
MAX_PROGRAMS = 100
# Programs whose first argument names what they do.
SUBCOMMANDS = frozenset(
    {"git", "gh", "docker", "kubectl", "npm", "pnpm", "yarn", "uv", "pip", "cargo", "go", "make", "evo-agents", "evo"}
)
VALUED_OPTIONS = {"git": ("-C", "-c", "--git-dir", "--work-tree"), "docker": ("--context", "-H")}
SKIPPED_PROGRAMS = frozenset({"cd", "pushd", "export", "source", ".", "set", "time", "env", "sudo", "nohup"})
SHELL_LEADS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "(", "time"})
SHELL_BLOCKS = frozenset({"for", "case", "select", "function"})  # the words after these name no program
SHELL_ENDS = frozenset({"fi", "done", "esac", "}", ")", "break", "continue"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
OPERATORS = "();<>|&\n"  # the characters of the shell's operators, a line end among them
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
EVO_AGENTS = "evo-agents"

KINDS = ("usage", "environment", "other")
KIND_FIELDS = {"usage": "usage_errors", "environment": "env_errors", "other": "other_errors"}
# What an exit status of evo-agents says (docs/hub.md, "Exit status"); 1 says nothing, and the output is read instead.
EXIT_KINDS = {EXIT_USAGE: "usage", EXIT_UNAVAILABLE: "environment", EXIT_TEMPFAIL: "environment"}
EXIT_LINE = re.compile(r"\A\s*Exit code (-?\d+)\b")  # the first line of a failed Bash result in Claude Code
USAGE = re.compile(
    r"^\s*usage:|unrecognized arguments|invalid choice|the following arguments are required|unrecognized option"
    r"|illegal option|unknown option",
    re.I | re.M,
)
TIMEOUT = re.compile(r"\btimed out\b|\bReadTimeout\b|\bTimeoutError\b|\bno answer within\b", re.I)

# The commands built into Claude Code, which a transcript writes as it writes a skill the person typed.
BUILTIN_COMMANDS = frozenset(
    {
        "add-dir", "agents", "bashes", "bug", "clear", "color", "compact", "config", "context", "cost", "doctor",
        "effort", "exit", "export", "fast", "feedback", "goal", "help", "hooks", "ide", "install-github-app",
        "keybindings", "login", "logout", "mcp", "memory", "migrate-installer", "model", "output-style",
        "permissions", "plugin", "plugins", "privacy-settings", "quit", "release-notes", "rename", "resume", "rewind",
        "sandbox", "status", "statusline", "tasks", "terminal-setup", "theme", "todos", "upgrade", "usage", "vim",
    }
)  # fmt: skip
COMMAND_NAME = re.compile(r"<command-name>\s*/?([^<>\s]+)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
INTERRUPTED = "[Request interrupted by user"  # what Claude Code writes when the person stops the agent
CODEX_SKILL = re.compile(r"\A\s*<skill>\s*<name>\s*([^<>\n]+?)\s*</name>")  # Codex's message of a skill named with $
SKILL_FIELDS = (
    "calls",
    "typed",
    "failed",
    "bash",
    "usage_errors",
    "env_errors",
    "other_errors",
    "interrupts",
    "corrections",
)


def cut(text, limit: int = MAX_NAME) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# The programs of a command


def _label(words: list[str]) -> str | None:
    """The program of one simple command's ``words``: its first word that is not ``cd``, a variable, a wrapper or a
    shell keyword, with the subcommand of the programs that take one; None when it runs none."""
    if not words or words[0] in SHELL_BLOCKS:
        return None
    words = list(words)
    while words and (ASSIGNMENT.fullmatch(words[0]) or words[0] in SKIPPED_PROGRAMS or words[0] in SHELL_LEADS):
        if words.pop(0) in ("cd", "pushd", "source", "."):
            return None  # its argument is a directory or a file, not a program
    if not words or words[0] in SHELL_ENDS:
        return None
    name = os.path.basename(words[0]) or words[0]
    given = words[1:]
    while given and given[0] in VALUED_OPTIONS.get(name, ()):
        given = given[2:]  # an option and its value, before the subcommand
    rest = [word for word in given if not word.startswith("-")]
    if name.startswith("python") and len(words) > 2 and words[1] == "-m":
        return cut(f"{name} -m {words[2]}")
    if name in SUBCOMMANDS and rest:
        return cut(f"{name} {rest[0]}")
    return cut(name)


def _without_heredocs(command: str) -> str:
    """``command`` without the lines of its here-documents, which are data, not commands."""
    kept, ends = [], []
    for line in command.split("\n"):
        if ends:
            if line.strip() == ends[0]:
                ends.pop(0)
            continue
        kept.append(line)
        ends.extend(found.group(2) for found in HEREDOC.finditer(line))
    return "\n".join(kept)


def _operator(token: str) -> bool:
    return bool(token) and all(char in OPERATORS for char in token)


def simple_commands(command: str) -> list[list[str]] | None:
    """The words of each simple command of a shell command line, split at its operators and line ends, the lines of
    here-documents and the targets of redirections left out; None for a line the shell would not read (an open
    quote)."""
    try:
        lexer = shlex.shlex(_without_heredocs(command), posix=True, punctuation_chars=OPERATORS)
        lexer.whitespace = " \t\r"  # a line end separates commands
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None
    groups: list[list[str]] = [[]]
    target = False  # the token after a redirection names a file
    for index, token in enumerate(tokens):
        if target:
            target = False
        elif _operator(token):
            target = "<" in token or ">" in token
            if not target:
                groups.append([])
        elif not (token.isdigit() and index + 1 < len(tokens) and _operator(tokens[index + 1])):
            groups[-1].append(token)  # a digit before an operator is the descriptor of 2>&1
    return [words for words in groups if words]


def programs(command: str) -> list[str]:
    """The programs a Bash command runs, each once, in order: the program (``_label``) of each simple command
    (``simple_commands``). A command the shell would not read gives the program of its first part, if any."""
    found = simple_commands(command or "")
    if found is None:
        first = re.split(r"&&|\|\||;|\||\n", command)[0].split()
        found = [first] if first else []
    return list(dict.fromkeys(label for words in found if (label := _label(words))))


def program(command: str) -> str | None:
    """The first program a Bash command runs (``programs``); None for a command without one."""
    found = programs(command)
    return found[0] if found else None


def _names(label: str) -> list[str]:
    """The names a program of ``programs`` writes its errors under: its own, and the module ``python -m`` runs."""
    words = label.split()
    return [words[0], words[2]] if len(words) > 2 and words[1] == "-m" else [words[0]]


def names_itself(label: str, text: str) -> bool:
    """Whether a line of ``text`` starts with the name of program ``label`` (a path before it, up to three words after
    it) and a colon: ``evo-agents kg scrub: error: ...``, ``ls: cannot access ...``."""
    return any(
        re.search(rf"^(?:\S*/)?{re.escape(name)}(?:[ \t]+[\w.-]+){{0,3}}:[ \t]", text, re.M) for name in _names(label)
    )


def blame(command: str, text: str) -> tuple[str | None, bool]:
    """The program a failure of ``command`` whose output is ``text`` counts for, and whether the command ran it alone;
    (None, False) when it ran several and the output names none of them."""
    found = programs(command)
    if len(found) <= 1:
        return (found[0] if found else "unknown"), True
    for label in found:
        if names_itself(label, text):
            return label, False
    return None, False


def exit_code(text: str) -> int | None:
    """The exit status a failed Bash result of Claude Code starts with (``Exit code N``), None when it has none."""
    found = EXIT_LINE.match(text or "")
    return int(found.group(1)) if found else None


def error_kind(text: str, alone: str | None = None, code: int | None = None) -> str:
    """The KINDS of a failure whose output is ``text``: by its exit status (``code``, else the ``Exit code N`` line)
    when the command ran evo-agents alone (``alone``, its program), else by the output."""
    code = exit_code(text) if code is None else code
    if alone is not None and alone.split()[0] == EVO_AGENTS and code in EXIT_KINDS:
        return EXIT_KINDS[code]
    if USAGE.search(text or ""):
        return "usage"
    if environment_cause(text or "") is not None or TIMEOUT.search(text or ""):
        return "environment"
    return "other"


class BashTally:
    """The Bash commands of a session per program: the calls of each program (a command that runs several counts a call
    for each), the failures that count for it (``blame``) by kind, and the failures of commands that ran several
    programs and whose output named none of them."""

    def __init__(self):
        self.calls: Counter = Counter()
        self.failed: Counter = Counter()
        self.kinds: dict[str, Counter] = defaultdict(Counter)
        self.compound: Counter = Counter()

    def call(self, command: str) -> list[str]:
        """Count ``command``; the programs it runs, ``unknown`` for a command that runs none."""
        found = programs(command) or ["unknown"]
        for label in found:
            self.calls[label] += 1
        if len(found) > 1:
            self.compound["calls"] += 1
        return found

    def fail(self, command: str, text: str, code: int | None = None) -> str:
        """Count a failure of ``command`` whose output is ``text``; its kind."""
        label, alone = blame(command, text)
        kind = error_kind(text, label if alone else None, code)
        if label is None:
            self.compound["errors"] += 1
            self.compound[KIND_FIELDS[kind]] += 1
        else:
            self.failed[label] += 1
            self.kinds[label][KIND_FIELDS[kind]] += 1
        return kind

    def program_figures(self) -> list[dict]:
        """``bash`` of the digest: per program, its calls and the failures that count for it, the most called first."""
        ranked = sorted(self.calls.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_PROGRAMS]
        return [{"program": name, "calls": n, "errors": min(self.failed[name], n)} for name, n in ranked]

    def program_kinds(self) -> list[dict]:
        """``bash_kinds`` of the digest: per program with failures, how many of each kind, the most failed first."""
        ranked = sorted(self.failed.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_PROGRAMS]
        return [
            {"program": name, **{field: self.kinds[name][field] for field in KIND_FIELDS.values()}}
            for name, _ in ranked
        ]

    def compound_figures(self) -> dict:
        """``bash_compound`` of the digest: the commands that ran several programs, and their failures that count for
        none, by kind."""
        return {"calls": self.compound["calls"], "errors": self.compound["errors"]} | {
            field: self.compound[field] for field in KIND_FIELDS.values()
        }


# The skills of a session


def typed_command(text: str) -> tuple[str, str] | None:
    """The command a message of the person typed and its arguments, or None: Claude Code writes such a message as
    ``<command-message>name</command-message>`` and ``<command-name>/name</command-name>``, either first."""
    found = COMMAND_NAME.search(text or "") if (text or "").lstrip().startswith("<command-") else None
    if found is None:
        return None
    args = COMMAND_ARGS.search(text)
    return found.group(1), (args.group(1).strip() if args else "")


def codex_skill(text: str) -> str | None:
    """The skill a Codex message of the person names when it starts with ``<skill>``, or None."""
    found = CODEX_SKILL.match(text or "")
    return found.group(1) if found else None


class SkillTally:
    """The skills one session loads, and per skill the friction in the windows its loads open: see the module."""

    def __init__(self):
        self.figures: dict[str, Counter] = {}
        self.open: set[str] = set()
        self.calls: dict[str, str] = {}  # tool call id: the skill it loads
        self.interrupts = 0
        self.turns = 0

    def load(self, name, typed: bool, call_id: str | None = None) -> None:
        """A load of skill ``name``: typed by the person, or through the Skill tool's call ``call_id``."""
        name = cut(name.strip()) if isinstance(name, str) else ""
        if not name:
            return
        found = self.figures.setdefault(name, Counter())
        found["calls"] += 1
        found["typed"] += typed
        if call_id is not None:
            self.calls[call_id] = name
        self.open.add(name)

    def load_failed(self, call_id: str) -> None:
        """The Skill tool's call ``call_id`` answered an error."""
        if call_id in self.calls:
            self.figures[self.calls[call_id]]["failed"] += 1

    def turn(self, text: str) -> None:
        """A turn of the person: it closes every window, and corrects the skills of the windows when it reads as one."""
        self.turns += 1
        if is_correction(text):
            for name in self.open:
                self.figures[name]["corrections"] += 1
        self.open.clear()

    def person(self, text: str) -> None:
        """What a message of the person in a Claude Code transcript means for the skills: an interrupt, a command it
        typed (a skill unless it is built in), or a turn; text the harness wrote (it starts with ``<``) is none."""
        if not text:
            return
        if text.startswith(INTERRUPTED):
            self.interrupt()
            return
        typed = typed_command(text)
        if typed is not None:
            self.turn(typed[1])
            if typed[0] not in BUILTIN_COMMANDS:
                self.load(typed[0], typed=True)
        elif not text.startswith("<"):
            self.turn(text)

    def interrupt(self) -> None:
        self.interrupts += 1
        for name in self.open:
            self.figures[name]["interrupts"] += 1

    def bash(self, kind: str | None = None) -> None:
        """A Bash command in the open windows, and its failure's kind when it failed (counted once it is known)."""
        field = "bash" if kind is None else KIND_FIELDS[kind]
        for name in self.open:
            self.figures[name][field] += 1

    def skills(self) -> list[dict]:
        """``skills`` of the digest, the most loaded first."""
        ranked = sorted(self.figures.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))[:MAX_SKILLS]
        return [{"skill": name, **{field: found[field] for field in SKILL_FIELDS}} for name, found in ranked]


def digest_fields(bash: BashTally, skills: SkillTally) -> dict:
    """What a session's tallies add to its digest: ``bash_kinds``, ``bash_compound``, ``skills``, ``interrupts`` and
    ``turns`` (the turns of the person)."""
    return {
        "bash_kinds": bash.program_kinds(),
        "bash_compound": bash.compound_figures(),
        "skills": skills.skills(),
        "interrupts": skills.interrupts,
        "turns": skills.turns,
    }
