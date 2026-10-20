"""What a plan's verify commands run, read from their shell words without running them: the files they name
(``verify_paths``), the files that say what a runner they call runs and the package.json scripts they run
(``verify_runners``), and the programs they call from PATH (``programs``). Pure functions, standard library only, so the
hub, the worker daemon and the tests share them; ``evo_agents.hub.judge`` reads a diff with the first two and the
worker's preflight (``evo_agents.worker.preflight``) looks for the programs on a run's PATH. The judge module names them
too, as it did before they moved here.
"""

from __future__ import annotations

import itertools
import re
import shlex

from evo_agents.hub import tiers

# The runners a verify command may call, and the files that say what each one runs.
RUNNER_FILES = {
    "make": ("Makefile", "**/Makefile", "makefile", "**/makefile", "GNUmakefile", "**/GNUmakefile", "**/*.mk"),
    "gmake": ("Makefile", "**/Makefile", "makefile", "**/makefile", "GNUmakefile", "**/GNUmakefile", "**/*.mk"),
    "just": ("justfile", "**/justfile", "Justfile", "**/Justfile", ".justfile", "**/.justfile"),
    "nox": ("noxfile.py", "**/noxfile.py"),
    "tox": ("tox.ini", "**/tox.ini"),
    "invoke": ("tasks.py", "**/tasks.py"),
    "inv": ("tasks.py", "**/tasks.py"),
    "task": ("Taskfile.yml", "**/Taskfile.yml", "Taskfile.yaml", "**/Taskfile.yaml"),
}
PACKAGE_RUNNERS = ("npm", "pnpm", "yarn", "bun")  # their scripts live in package.json
# Words before the command a verify command runs: wrappers that run what follows.
WRAPPERS = {"uv": ("run",), "poetry": ("run",), "pipenv": ("run",), "hatch": ("run",), "pdm": ("run",)}
PASS_THROUGH = ("env", "time", "nice", "exec", "command", "npx", "timeout", "xvfb-run")
SHELL_OPERATOR = re.compile(r"&&|\|\||[;|&\n()]")
# What ``programs`` reads in a command line. The shell's builtins and the words that end a compound command run no
# program from PATH; after a keyword that starts one (if, while, !, ...) the next word is the program; a simple command
# that may change PATH (source, ., eval, export PATH=..., PATH=... alone) ends the reading of the line.
SHELL_BUILTINS = frozenset(
    {
        ":", "[", "[[", "alias", "bg", "break", "cd", "continue", "declare", "echo", "exit", "export", "false", "fg",
        "getopts", "hash", "jobs", "kill", "let", "local", "popd", "printf", "pushd", "pwd", "read", "readonly",
        "return", "set", "shift", "test", "trap", "true", "type", "typeset", "ulimit", "umask", "unalias", "unset",
        "wait", "for", "case", "select", "function", "in", "fi", "done", "esac", "}", "]]",
    }
)  # fmt: skip
SHELL_LEADS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{", "time", "exec", "command"})
SHELL_PATH_CHANGES = frozenset({"source", ".", "eval"})
# Programs that run the program after them: the options of each that take a value, and whether a word (the duration
# of timeout) comes before that program.
RUNS_ANOTHER: dict[str, tuple[frozenset[str], bool]] = {
    "env": (frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}), False),
    "nice": (frozenset({"-n", "--adjustment"}), False),
    "nohup": (frozenset(), False),
    "timeout": (frozenset({"-s", "--signal", "-k", "--kill-after"}), True),
    "xvfb-run": (
        frozenset({"-e", "--error-file", "-f", "--auth-file", "-n", "--server-num", "-p", "-s", "--server-args", "-w"}),
        False,
    ),
}
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.S)


def verify_paths(commands: list[str]) -> set[str]:
    """The files the verify commands name: each word that looks like a path, without a leading ``./``."""
    found = set()
    for command in commands or []:
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        for word in words:
            word = word.split("::", 1)[0]  # pytest's tests/test_x.py::test_y
            if word.startswith("-") or "=" in word or "$" in word:
                continue
            if "/" in word or re.search(r"\.[A-Za-z0-9]{1,5}$", word) or word in ("Makefile", "justfile"):
                normal = tiers.normalize_path(word)
                if normal is not None:
                    found.add(normal)
    return found


def _simple_commands(command: str) -> list[list[str]]:
    """The words of each simple command of a shell command line, split at its operators; words of a quoted
    ``sh -c`` or ``bash -c`` argument read as commands too."""
    found: list[list[str]] = []
    try:
        lexer = shlex.shlex(command or "", posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:  # an open quote: read it as words between the operators
        tokens = [word for part in SHELL_OPERATOR.split(command or "") for word in [*part.split(), ";"]]
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token and all(char in "&|;()<>" for char in token):
            groups.append([])
        else:
            groups[-1].append(token)
    for words in groups:
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):  # FOO=bar before the command
            words = words[1:]
        if not words:
            continue
        if words[0].rsplit("/", 1)[-1] in ("sh", "bash", "zsh", "dash") and "-c" in words[1:-1]:
            found += _simple_commands(words[words.index("-c") + 1])
            continue
        found.append(words)
    return found


def _shell_words(command: str) -> list[list[str]] | None:
    """The words of each simple command of a shell command line, split at its operators, a redirection's target left
    out, assignments before a command kept; words of a quoted ``sh -c`` argument are not read here. None for a line the
    shell would not read (an open quote)."""
    try:
        lexer = shlex.shlex(command or "", posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return None

    def redirection(token: str) -> bool:
        return bool(token) and all(char in "&|;()<>" for char in token) and ("<" in token or ">" in token)

    groups: list[list[str]] = [[]]
    target = False  # the token after a redirection names a file
    for index, token in enumerate(tokens):
        if target:
            target = False
            continue
        if token and all(char in "&|;()<>" for char in token):
            if redirection(token):
                target = True
            else:
                groups.append([])
            continue
        if token.isdigit() and index + 1 < len(tokens) and redirection(tokens[index + 1]):
            continue  # the descriptor of 2>&1
        groups[-1].append(token)
    return [words for words in groups if words]


def _past_options(head: str, words: list[str]) -> tuple[list[str], bool]:
    """(the words after the options of ``head``, a program of RUNS_ANOTHER, and its duration for timeout: the program
    it runs and that program's arguments; whether ``env`` set PATH for that program)."""
    valued, duration = RUNS_ANOTHER[head]
    rest, path_set = list(words), False
    while rest and (rest[0].startswith("-") or (head == "env" and ASSIGNMENT.fullmatch(rest[0]))):
        option = rest.pop(0)
        if option == "--":
            break
        path_set = path_set or option.startswith("PATH=")
        if option in valued and rest:
            rest.pop(0)
    return (rest[1:] if duration and rest else rest), path_set


def programs(command: str) -> list[str]:
    """The programs a shell command line runs from PATH, each once, in order: the first word of each simple command
    (past assignments, and past the keywords that start a compound command), and of the program a wrapper of
    RUNS_ANOTHER runs (``timeout 60 pytest`` names timeout and pytest), and of a ``sh -c`` argument. Not counted: the
    shell's builtins and keywords, a word the shell expands (``$``, a backtick, a glob), a relative path (a file of the
    worktree, which the preflight cannot see yet), the program of a command with ``PATH=...`` before it, and anything
    after a simple command that may change PATH (``source``, ``.``, ``eval``, ``export PATH=...``, ``PATH=...``). An
    absolute path counts as itself. ``python -m pytest`` names python, and ``uv run pytest`` uv: what those run is not
    looked for on PATH."""
    found: list[str] = []
    for words in _shell_words(command) or []:
        assigned = [word.split("=", 1)[0] for word in itertools.takewhile(ASSIGNMENT.fullmatch, words)]
        rest = words[len(assigned) :]
        if "PATH" in assigned and not rest:
            break
        while rest:
            head = rest[0]
            if head in SHELL_PATH_CHANGES or (head == "export" and any(w.startswith("PATH=") for w in rest[1:])):
                return list(dict.fromkeys(found))
            if head in SHELL_LEADS:
                rest = rest[1:]
                continue
            if head in SHELL_BUILTINS or "PATH" in assigned or re.search(r"[$`*?\[\]]", head):
                break
            if "/" in head and not head.startswith("/"):
                break
            name = head.rsplit("/", 1)[-1]
            found.append(head)
            if name in ("sh", "bash", "zsh", "dash") and "-c" in rest[1:-1]:
                found += programs(rest[rest.index("-c") + 1])
                break
            if name not in RUNS_ANOTHER:
                break
            rest, path_set = _past_options(name, rest[1:])
            if path_set:
                break
    return list(dict.fromkeys(found))


def _runner(words: list[str]) -> tuple[str | None, list[str]]:
    """(the program a simple command runs, its arguments), past the wrappers that run another program."""
    while words:
        head = words[0].rsplit("/", 1)[-1]
        if head in PASS_THROUGH:
            words = [word for word in words[1:] if not word.startswith("-")] if head != "timeout" else words[2:]
            continue
        if head in WRAPPERS and len(words) > 1 and words[1] in WRAPPERS[head]:
            words = words[2:]
            continue
        if re.fullmatch(r"python[0-9.]*", head) and len(words) > 2 and words[1] == "-m":
            return words[2], words[3:]
        return head, words[1:]
    return None, []


def verify_runners(commands: list[str]) -> tuple[set[str], set[str]]:
    """(globs of the files that say what the verify commands run, such as the Makefile of ``make check``; the names of
    the package.json scripts they run, such as ``test`` of ``pnpm test``)."""
    globs: set[str] = set()
    scripts: set[str] = set()
    for command in commands or []:
        for words in _simple_commands(command):
            program, args = _runner(words)
            if program is None:
                continue
            globs.update(RUNNER_FILES.get(program, ()))
            if program in PACKAGE_RUNNERS:
                args = [arg for arg in args if not arg.startswith("-")]
                if args[:1] == ["run"]:
                    args = args[1:]
                if args and args[0] not in ("exec", "dlx", "install", "ci", "add", "x"):
                    scripts.add(args[0])
                elif not args:
                    scripts.add("test" if program == "npm" else "install")
    return globs, scripts
