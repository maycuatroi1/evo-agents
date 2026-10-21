"""What a plan's verify commands run, read from their shell words without running them: the files they name
(``verify_paths``), the files that say what a runner they call runs and the package.json scripts they run
(``verify_runners``), the programs they call from PATH (``programs``), and whether a line can be a shell command at all
(``shell_problem``). Pure functions, standard library only, so the hub, the worker daemon and the tests share them;
``evo_agents.hub.judge`` reads a diff with the first two, the worker's preflight (``evo_agents.worker.preflight``) looks
for the programs on a run's PATH, and the hub refuses a plan whose new verify ``shell_problem`` finds fault with
(``evo_agents.hub.plans.unrunnable_verify``). The judge module names them too, as it did before they moved here.
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
# The options of the leads that take some (``time -p``, ``exec -a NAME``, ``command -p``), with those that take a value;
# ``command -v`` and ``-V`` only look the name up and run nothing.
LEAD_OPTIONS = {"time": frozenset(), "exec": frozenset({"-a"}), "command": frozenset()}
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
SUBSTITUTED = "$_"  # what ``_substituted`` leaves of a ``$(...)`` or ``<(...)``: a word the shell expands
# The start of a here-document: <<, or <<- that strips leading tabs, and its delimiter, quoted or not.
HEREDOC = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|\\?([^\s;&|()<>'"]+))""")
# The name of a program a verify command may run (an absolute path's last part): ASCII letters, digits and _ . + -,
# starting with a letter, a digit or _ and ending with one of them or + (g++). Not a word of prose: Từ, a word with a
# trailing : , or ., a dash of a list.
COMMAND_NAME = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.+-]*[A-Za-z0-9_+])?")


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


def _closing(text: str, start: int) -> int | None:
    """The index of the ``)`` that closes the ``(`` at ``start`` of ``text``, past quoted text, escapes and nested
    parentheses; None when none does."""
    depth, quote, index = 0, None, start
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = None if char == "'" else quote
        elif char == "\\":
            index += 1
        elif quote == '"':
            quote = None if char == '"' else quote
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _substituted(command: str) -> tuple[str, list[str]]:
    """``command`` as shlex can read it, and the command lines it runs inside: each ``$(...)`` (in double quotes too),
    ``<(...)`` and ``>(...)`` becomes the one word SUBSTITUTED and its command line goes to the second list (not the
    expression of an arithmetic ``$((...))``); a newline outside quotes ends a command as ``;`` does, a backslash
    before one continues the line, and a comment and the body of a here-document are left out. Read as they are, the
    parentheses of ``git diff $(git merge-base a b) -- f`` would end a simple command and ``--`` would start one, the
    quotes of ``"$(python -c "print(1)")"`` would close early, and a here-document's lines would read as commands."""
    out: list[str] = []
    inner: list[str] = []
    heredocs: list[tuple[str, bool]] = []  # (delimiter, leading tabs stripped) of each here-document opened
    quote, index = None, 0
    while index < len(command):
        char = command[index]
        if quote is None and char == "#" and (index == 0 or command[index - 1] in " \t\n;&|()<>"):
            end = command.find("\n", index)  # a comment, to the end of its line
            index = len(command) if end == -1 else end
            continue
        if quote != "'" and command.startswith("(", index + 1) and (char == "$" or (quote is None and char in "<>")):
            end = _closing(command, index + 1)
            if end is not None:
                body = command[index + 2 : end]
                if not (char == "$" and body.startswith("(")):
                    inner.append(body)
                out.append(SUBSTITUTED)
                index = end + 1
                continue
        heredoc = HEREDOC.match(command, index) if quote is None and char == "<" else None
        if heredoc is not None and not command.startswith("<<<", index):
            word = next(group for group in heredoc.groups()[1:] if group is not None)
            heredocs.append((word, heredoc.group(1) == "-"))
            out.append(heredoc.group(0))
            index = heredoc.end()
            continue
        if quote is None and char == "\n":  # a newline ends a command, as ; does
            out.append(";")
            index += 1
            for word, tabs in heredocs:  # each body ends at a line that is its delimiter alone
                while index < len(command):
                    end = command.find("\n", index)
                    end = len(command) if end == -1 else end
                    line, index = command[index:end], end + 1
                    if (line.lstrip("\t") if tabs else line) == word:
                        break
            heredocs = []
            continue
        if char == "\\" and quote != "'":
            escaped = command[index : index + 2]
            out.append(" " if escaped == "\\\n" and quote is None else escaped)  # a line continued
            index += 2
            continue
        if quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
        out.append(char)
        index += 1
    return "".join(out), inner


def _shell_words(command: str) -> list[list[str]] | None:
    """The words of each simple command of a shell command line, split at its operators, a redirection's target left
    out, assignments before a command kept, then those of the command lines it runs inside, such as a ``$(...)``
    (``_substituted``); words of a quoted ``sh -c`` argument are not read here. None for a line the shell would not
    read (an open quote)."""
    line, inner = _substituted(command or "")
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
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
    found = [words for words in groups if words]
    for each in inner:
        found += _shell_words(each) or []
    return found


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


def _past_lead(head: str, words: list[str]) -> tuple[list[str], bool]:
    """(the words after the lead ``head`` of SHELL_LEADS and its options, LEAD_OPTIONS; whether it only looks a name
    up and runs nothing, as ``command -v`` does)."""
    valued = LEAD_OPTIONS.get(head)
    rest = list(words)
    while valued is not None and rest and rest[0].startswith("-") and rest[0] != "-":
        option = rest.pop(0)
        if option == "--":
            break
        if head == "command" and not option.startswith("--") and {"v", "V"} & set(option[1:]):
            return rest, True
        if option in valued and rest:
            rest.pop(0)
    return rest, False


def programs(command: str) -> list[str]:
    """The programs a shell command line runs from PATH, each once, in order: the first word of each simple command
    (past assignments, and past the keywords that start a compound command and the options of LEAD_OPTIONS; nothing
    after ``command -v``), of the program a wrapper of RUNS_ANOTHER runs (``timeout 60 pytest`` names timeout and
    pytest), of a ``sh -c`` argument and of a ``$(...)`` (``_shell_words``). Not counted: the shell's builtins and
    keywords, a word the shell expands (``$``, a backtick, a glob), a relative path (a file of the worktree, which the
    preflight cannot see yet), the program of a command with ``PATH=...`` before it, and anything after a simple
    command that may change PATH (``source``, ``.``, ``eval``, ``export PATH=...``, ``PATH=...``). An absolute path
    counts as itself. ``python -m pytest`` names python, and ``uv run pytest`` uv: what those run is not looked for on
    PATH."""
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
                rest, looked_up = _past_lead(head, rest[1:])
                if looked_up:
                    break
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


def shell_problem(command: str) -> str | None:
    """Why ``command`` cannot be one runnable shell command line, read by its shape alone, or None when it can be: the
    shell would not read it (a quote left open, as an apostrophe of prose leaves one), or a program it runs
    (``programs``) has no COMMAND_NAME, as Từ, ``Manual:`` or ``1.`` have not; an absolute path is read by its last part.
    Whether a program is on a worker's PATH is the preflight's to see, not this. The phrase goes after "the verify of
    step N"."""
    if _shell_words(command) is None:
        return "does not parse as shell: a quote is left open (an apostrophe in prose opens one)"
    bad = [program for program in programs(command) if not COMMAND_NAME.fullmatch(program.rsplit("/", 1)[-1])]
    if not bad:
        return None
    named = ", ".join(f"`{program}`" for program in bad)
    if len(bad) == 1:
        return f"runs {named} as a program, and that is not a command name"
    return f"runs {named} as programs, and those are not command names"


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
