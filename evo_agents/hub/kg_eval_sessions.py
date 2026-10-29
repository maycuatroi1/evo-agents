"""Questions for ``evo-agents kg eval`` from how agents searched the code (``kg eval-questions --from-sessions``).

The questions of ``evo_agents.hub.kg_eval`` are the titles of plan steps, which the graph holds word for word as Step
nodes, so they flatter it. These come from the Claude Code transcripts of the harness instead. A Bash call that
searches one sibling repo (``git grep``, ``grep -r``/``-R`` or ``rg``, after a ``cd`` into the repo, with ``git -C
REPO`` or on a path in it) asks the identifiers its patterns name, and the files of that repo the agent opened in the
next WINDOW tool calls answer it: a Read of a path in the repo or one of its worktrees, ``git show REF:PATH`` there,
``sed -n``, ``cat``, ``head`` or ``tail`` of a file there. A relative path counts from the directory the call ran in
(the transcript's ``cwd``, then each ``cd`` before it in the same command), so a harness file of the same name never
answers for the repo.

Transcripts: those of sessions started in the harness root, a directory under it (Claude Code's
``.claude/worktrees/<name>``) or another git worktree of the harness, in the Claude Code projects directories given
(``evo_agents.hub.transcripts.claude_roots`` on this machine): ``<slug>/<session>.jsonl`` and the subagents' under
``<slug>/<session>/subagents/``, their tool calls since ``since``. Repos: those a source of knowledge.yaml reads (its
``repo``), other than the harness itself, under their harness.yaml names, so an answer ``repo:path`` names a file as
the graph does. A worktree of a repo is a ``git worktree list`` entry of its checkout, ``<checkout>/.claude/worktrees/
<name>``, ``<checkout>-wt/<name>`` or ``<checkout>-wt-<name>``.

A pattern gives its terms: the identifiers in it once regex syntax is taken out, MIN_TERM characters or more, without
the keywords a search puts before a name (``def``, ``class``) or pure numbers, at most MAX_TERMS a question. A search
none of whose terms looks like code (snake_case, camelCase, CONSTANT, dotted, ``::`` or kebab names, digits) gives no
question. An answer is left out when it is a lockfile or .json, or when it is in neither the files git tracks in the
repo's checkout nor the tree of a ref its sources read. The same terms searched in the same repo again make one
question of type SESSION_TYPE, whose id is a hash of the repo and the terms, so its part (tune or holdout) holds as
sessions come and go; its answers are the files opened most often after them, at most MAX_ANSWERS.

Only reads: the transcripts, and ``git worktree list``, ``ls-files`` and ``ls-tree`` in the checkouts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from evo_agents.hub.memory import slug
from evo_agents.kg.evaluate import SESSION_TYPE
from evo_agents.kg.redact import redact_file

WINDOW = 6  # tool calls after a search whose opened files answer it
MAX_TERMS = 4
MIN_TERM = 4
MAX_TERM = 80
MAX_ANSWERS = 3
ID_PREFIX = "session-"
GIT_TIMEOUT = 30.0
LOCKFILES = frozenset(
    "uv.lock poetry.lock Pipfile.lock yarn.lock pnpm-lock.yaml package-lock.json Cargo.lock Gemfile.lock composer.lock "
    "go.sum pubspec.lock Podfile.lock flake.lock bun.lockb".split()
)
KEYWORDS = frozenset(
    "class def function const export import from return self this async await interface struct type func public "
    "private protected static final void null none true false enum impl trait module package".split()
)
TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:(?:\.|::|-)[A-Za-z0-9_]+)*")
REGEX_SYNTAX = re.compile(r"\\[wWsSdDbBAZz<>]|\[[^\]]*\]")  # classes and anchors: no identifier inside
ESCAPED = re.compile(r"\\(.)")
CODE_SHAPED = re.compile(r"[_.:0-9-]|[a-z][A-Z]")
HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_]\w*)\1")
HOME_VARIABLE = re.compile(r"\$\{HOME\}|\$HOME\b")
OPERATOR_CHARS = frozenset("();<>|&\n")
SEPARATOR, PIPE, REDIRECT = "separator", "pipe", "redirect"
PREFIX_WORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "time", "command", "nohup"})
RECURSIVE = frozenset({"-r", "-R", "--recursive", "--dereference-recursive"})
GLOB_CHARS = frozenset("*?[")


@dataclass(frozen=True)
class Options:
    """The options of one command that take a value: short letters and long names; ``-e`` and ``--regexp`` are
    patterns, ``-f`` and ``--file`` patterns from a file."""

    short: str
    long: frozenset = frozenset()


GIT_GREP = Options(
    "efmABC", frozenset({"--max-depth", "--max-count", "--threads", "--context", "--after-context", "--before-context"})
)
GREP = Options(
    "efmABCdD",
    frozenset(
        "--regexp --file --include --exclude --exclude-dir --exclude-from --max-count --context --after-context "
        "--before-context --label --binary-files --devices --directories".split()
    ),
)
RG = Options(
    "efgtTABCmMjrdE",
    frozenset(
        "--regexp --file --glob --iglob --type --type-not --after-context --before-context --context --max-count "
        "--max-columns --threads --replace --max-depth --encoding --type-add --sort --sortr --pre --pre-glob "
        "--max-filesize --engine --ignore-file".split()
    ),
)
SED = Options("efl", frozenset({"--expression", "--file", "--line-length"}))
HEAD = Options("nc", frozenset({"--lines", "--bytes"}))
CAT = Options("")
READERS = {"cat": CAT, "head": HEAD, "tail": HEAD, "sed": SED}
GIT_VALUES = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"})


# -- the shell -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One simple command of a shell command line: its words, the directory it runs in (None when not known) and
    whether it reads the output of a pipe."""

    argv: tuple[str, ...]
    cwd: str | None
    piped: bool = False


@dataclass
class Parsed:
    """One command's words: the positional ones, the patterns of ``-e``/``--regexp``, the flags named."""

    positional: list = field(default_factory=list)
    patterns: list = field(default_factory=list)
    flags: set = field(default_factory=set)


def _strip_heredocs(command: str) -> str:
    """The command without the bodies of its here-documents, which are text, not commands."""
    lines, kept, index = command.split("\n"), [], 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC.finditer(line):
            while index < len(lines) and lines[index].strip() != match.group(2):
                index += 1
            index += 1
    return "\n".join(kept)


def _operator(token: str) -> str | None:
    if not token or not set(token) <= OPERATOR_CHARS:
        return None
    if "<" in token or ">" in token:
        return REDIRECT
    return PIPE if token.strip("\n") in ("|", "|&") else SEPARATOR


def _unprefixed(words: list[str]) -> list[str]:
    """The words of a command without the variable assignments and keywords before its name."""
    index = 0
    while index < len(words) and (words[index] in PREFIX_WORDS or re.match(r"[A-Za-z_]\w*=", words[index])):
        index += 1
    return words[index:]


def simple_commands(command: str) -> list[tuple[list[str], bool]]:
    """The simple commands of a command line in order, each as its words and whether a pipe feeds it, with the
    redirections taken out; [] when the line does not parse (an open quote)."""
    lexer = shlex.shlex(_strip_heredocs(command).replace("\\\n", " "), posix=True, punctuation_chars="();<>|&\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    found, words, piped = [], [], False
    while tokens:
        token = tokens.pop(0)
        kind = _operator(token)
        if kind is None:
            words.append(token)
        elif kind == REDIRECT:
            if words and words[-1].isdigit():
                words.pop()  # the 2 of 2>/dev/null
            if tokens:
                tokens.pop(0)  # its target
        else:
            if words:
                found.append((words, piped))
            words, piped = [], kind == PIPE
    if words:
        found.append((words, piped))
    return [(command, piped) for words, piped in found if (command := _unprefixed(words))]


def resolve(path: str, cwd: str | None) -> str | None:
    """``path`` as an absolute normalized path, ``~`` and ``$HOME`` expanded and a relative one taken from ``cwd``;
    None when it names a variable or a command's output, or is relative to an unknown directory."""
    path = HOME_VARIABLE.sub(lambda _: str(Path.home()), path)
    if "$" in path or "`" in path or not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        if cwd is None:
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def steps(command: str, cwd: str | None) -> Iterator[Step]:
    """The simple commands of a command line with the directory each runs in, following its ``cd``s."""
    for words, piped in simple_commands(command):
        if words[0] in ("cd", "pushd"):
            target = words[1] if len(words) > 1 else None
            cwd = resolve(target, cwd) if target and target != "-" else None
            continue
        yield Step(tuple(words), cwd, piped)


def parse_options(args: Iterable[str], options: Options) -> Parsed:
    """The positional words, the patterns given with ``-e`` or ``--regexp`` and the flags of one command's words."""
    parsed, words, rest = Parsed(), list(args), False
    while words:
        word = words.pop(0)
        if rest or word == "-" or not word.startswith("-"):
            parsed.positional.append(word)
        elif word == "--":
            rest = True
        elif word.startswith("--"):
            name, given, value = word.partition("=")
            parsed.flags.add(name)
            if name in options.long and not given:
                value = words.pop(0) if words else ""
            if name == "--regexp":
                parsed.patterns.append(value)
        else:
            for index, letter in enumerate(word[1:], 1):
                parsed.flags.add(f"-{letter}")
                if letter in options.short:
                    value = word[index + 1 :] or (words.pop(0) if words else "")
                    if letter == "e":
                        parsed.patterns.append(value)
                    break
    return parsed


def _git(args: tuple[str, ...], cwd: str | None) -> tuple[str | None, list[str], str | None]:
    """(the subcommand, its words, the directory it runs in) of a ``git`` command's words, through ``-C``."""
    words = list(args)
    while words and words[0].startswith("-"):
        option = words.pop(0)
        if option in GIT_VALUES and words:
            value = words.pop(0)
            if option == "-C":
                cwd = resolve(value, cwd)
    return (words[0], words[1:], cwd) if words else (None, [], cwd)


# -- repos and their worktrees --------------------------------------------------------------------------------------


def _git_lines(checkout: Path | str, *args: str) -> list[str] | None:
    """The NUL- or line-separated output of a read-only git command, or None when it fails."""
    try:
        result = subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    return [part for part in text.split("\0" if "-z" in args else "\n") if part]


def worktrees(checkout: Path | str) -> list[str]:
    """The other worktrees of a checkout, as ``git worktree list`` names them."""
    listed = _git_lines(checkout, "worktree", "list", "--porcelain") or []
    own = os.path.realpath(checkout)
    paths = [line[len("worktree ") :] for line in listed if line.startswith("worktree ")]
    return [path for path in paths if os.path.realpath(path) != own]


def _spellings(path: Path | str) -> set[str]:
    return {os.path.normpath(os.path.expanduser(str(path))), os.path.realpath(os.path.expanduser(str(path)))}


@dataclass(frozen=True)
class RepoMap:
    """Where the repos are: an absolute path to (repo, the path in it, "" for its root), through its worktrees."""

    rules: tuple = ()  # (compiled pattern with group "path", repo), the longest directory first

    def locate(self, path: str | None) -> tuple[str, str] | None:
        if not path:
            return None
        for rule, repo in self.rules:
            match = rule.match(path)
            if match:
                return repo, match.group("path") or ""
        return None


def repo_map(checkouts: dict[str, Path]) -> RepoMap:
    tail = r"(?:/(?P<path>.*))?\Z"
    rules: list[tuple[int, re.Pattern, str]] = []
    for repo, checkout in checkouts.items():
        for base in _spellings(checkout):
            nested = r"(?:/\.claude/worktrees/[^/]+|-wt/[^/]+|-wt-[^/]+)?"
            rules.append((len(base), re.compile(re.escape(base) + nested + tail), repo))
        for tree in worktrees(checkout):
            rules += [(len(base), re.compile(re.escape(base) + tail), repo) for base in _spellings(tree)]
    rules.sort(key=lambda rule: -rule[0])
    return RepoMap(tuple((rule, repo) for _, rule, repo in rules))


def tracked_files(checkout: Path, refs: Iterable[str]) -> frozenset[str] | None:
    """The files git tracks in a checkout and in the tree of each ref it has; None when it is no checkout."""
    found = _git_lines(checkout, "ls-files", "-z")
    if found is None:
        return None
    for ref in refs:
        found += _git_lines(checkout, "ls-tree", "-r", "-z", "--name-only", ref) or []
    return frozenset(found)


def source_repos(project, overrides: dict[str, Path] | None = None) -> dict[str, tuple[Path, tuple[str, ...]]]:
    """The repos the project's sources read, by harness.yaml name, other than the harness: (checkout, the refs read).
    ``overrides`` gives the checkout of a repo by name instead of harness.yaml."""
    harness, overrides = project.harness, overrides or {}
    root = os.path.realpath(harness.root)
    found: dict[str, tuple[Path, list[str]]] = {}
    for source in project.sources():
        name = source.get("repo")
        if not isinstance(name, str) or not name:
            continue
        entry = harness.repo(name)
        checkout = overrides.get(name) or (harness.repo_path(entry) if entry else None)
        if checkout is None or os.path.realpath(checkout) == root:
            continue
        refs = found.setdefault(name, (Path(checkout), []))[1]
        if isinstance(source.get("ref"), str) and source["ref"] not in refs:
            refs.append(source["ref"])
    return {name: (checkout, tuple(refs)) for name, (checkout, refs) in found.items()}


# -- searches and the files opened after them ----------------------------------------------------------------------


def terms_of(patterns: Iterable[str]) -> list[str]:
    """The identifiers a search's patterns name, in order, each once whatever its case; never one the redaction of
    the corpus would take for a secret (an agent may grep for a token's value), since the set goes to the hub."""
    found, seen = [], set()
    for pattern in patterns:
        for term in TERM.findall(ESCAPED.sub(r"\1", REGEX_SYNTAX.sub(" ", pattern))):
            low = term.lower()
            if MIN_TERM <= len(term) <= MAX_TERM and low not in KEYWORDS and low not in seen:
                if not term.strip("_").isdigit() and not redact_file(term, "terms.txt")[1]:
                    seen.add(low)
                    found.append(term)
    return found


def code_shaped(term: str) -> bool:
    """Whether a term looks like a name in code rather than a word: snake_case, camelCase, CONSTANT, dotted, ``::`` or
    kebab names, digits."""
    return bool(CODE_SHAPED.search(term)) or term.isupper()


def search_of(step: Step, repos: RepoMap) -> tuple[str, list[str]] | None:
    """(repo, patterns) when a simple command searches the code of one repo, else None."""
    name, cwd = os.path.basename(step.argv[0]), step.cwd
    if name == "git":
        sub, args, cwd = _git(step.argv[1:], cwd)
        if sub != "grep":
            return None
        parsed = parse_options(args, GIT_GREP)
    elif name in ("grep", "egrep", "fgrep"):
        parsed = parse_options(step.argv[1:], GREP)
        if not parsed.flags & RECURSIVE:
            return None  # a filter of other output, not a search of the code
    elif name == "rg" and not step.piped:
        parsed = parse_options(step.argv[1:], RG)
    else:
        return None
    patterns, positional = parsed.patterns, parsed.positional
    if not patterns:
        if "-f" in parsed.flags or "--file" in parsed.flags or not positional:
            return None
        patterns, positional = positional[:1], positional[1:]
    # git grep's other words are revisions and pathspecs of the repo it runs in; grep's and rg's are paths
    places = [cwd] if name == "git" or not positional else [resolve(path, cwd) for path in positional]
    located = {repos.locate(place) for place in places}
    named = {hit[0] for hit in located if hit is not None}
    return (named.pop(), patterns) if None not in located and len(named) == 1 else None


def _shown(args: list[str], cwd: str | None, repos: RepoMap) -> list[tuple[str, str]]:
    """The files ``git show REF:PATH`` prints, run in ``cwd``."""
    root = repos.locate(cwd)
    found = []
    for arg in args:
        if root is None or arg.startswith("-") or ":" not in arg:
            continue
        path = arg.split(":", 1)[1]
        if not path or "$" in path or path.startswith("/"):
            continue
        hit = repos.locate(resolve(path, cwd)) if path.startswith(("./", "../")) else (root[0], os.path.normpath(path))
        if hit is not None and hit[0] == root[0] and hit[1]:
            found.append(hit)
    return found


def _read(name: str, args: tuple[str, ...]) -> list[str]:
    """The files ``sed -n``, ``cat``, ``head`` or ``tail`` reads, as written."""
    parsed = parse_options(args, READERS[name])
    files = parsed.positional
    if name == "sed":
        if not parsed.flags & {"-n", "--quiet", "--silent"} or parsed.flags & {"-i", "--in-place"}:
            return []
        if not parsed.patterns and not parsed.flags & {"-f", "--file", "--expression"}:
            files = files[1:]  # the first word is the script
    return [path for path in files if path != "-" and not GLOB_CHARS & set(path)]


def opened(call: Call, repos: RepoMap) -> list[tuple[str, str]]:
    """The files of the repos one tool call opened: (repo, path in it)."""
    if call.name == "Read":
        path = call.input.get("file_path")
        hit = repos.locate(resolve(path, call.cwd)) if isinstance(path, str) else None
        return [hit] if hit is not None and hit[1] else []
    command = call.input.get("command")
    if call.name != "Bash" or not isinstance(command, str):
        return []
    found: list[tuple[str, str]] = []
    for step in steps(command, call.cwd):
        name = os.path.basename(step.argv[0])
        if name == "git":
            sub, args, cwd = _git(step.argv[1:], step.cwd)
            found += _shown(args, cwd, repos) if sub == "show" else []
        elif name in READERS:
            hits = [repos.locate(resolve(path, step.cwd)) for path in _read(name, step.argv[1:])]
            found += [hit for hit in hits if hit is not None and hit[1]]
    return found


def searched(call: Call, repos: RepoMap) -> dict[str, list[str]]:
    """The terms one Bash call searched, by repo, in order."""
    command = call.input.get("command")
    if call.name != "Bash" or not isinstance(command, str):
        return {}
    patterns: dict[str, list[str]] = {}
    for step in steps(command, call.cwd):
        search = search_of(step, repos)
        if search is not None:
            patterns.setdefault(search[0], []).extend(search[1])
    return {repo: terms_of(found) for repo, found in patterns.items()}


def answerable(path: str, known: frozenset[str] | None) -> bool:
    """Whether a file opened can answer: no lockfile or .json, and tracked when the tracked files are ``known``."""
    name = os.path.basename(path)
    if name in LOCKFILES or name.endswith((".lock", ".json")):
        return False
    return known is None or path in known


# -- transcripts ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One tool call of a transcript: the tool, its input and the directory the session was in."""

    name: str
    input: dict
    cwd: str | None


def _after(timestamp, since: float) -> bool:
    """Whether a line's timestamp is at or after ``since``; a line without one that parses counts as after."""
    if not isinstance(timestamp, str):
        return True
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() >= since
    except ValueError:
        return True


def read_calls(path: Path, since: float) -> tuple[str | None, list[Call]]:
    """The directory a transcript's session started in, and its tool calls made at or after ``since``, in order."""
    first, calls, seen = None, [], set()
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None, []
    with handle:
        for line in handle:
            if '"tool_use"' not in line and (first is not None or '"cwd"' not in line):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            cwd = entry.get("cwd") if isinstance(entry.get("cwd"), str) else None
            first = first or cwd
            message = entry.get("message")
            if (
                entry.get("type") != "assistant"
                or not isinstance(message, dict)
                or not _after(entry.get("timestamp"), since)
            ):
                continue
            for block in message.get("content") if isinstance(message.get("content"), list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("id") in seen:
                    continue
                if isinstance(block.get("input"), dict):
                    seen.add(block.get("id"))
                    calls.append(Call(str(block.get("name")), block["input"], cwd))
    return first, calls


def harness_places(root: Path) -> list[str]:
    """The harness root and its other git worktrees: where its sessions start."""
    return sorted({*_spellings(root), *(path for tree in worktrees(root) for path in _spellings(tree))})


def _inside(path: str | None, places: Iterable[str]) -> bool:
    return path is not None and any(path == place or path.startswith(place.rstrip("/") + "/") for place in places)


def transcripts(roots: Iterable[Path], places: list[str], since: float) -> list[Path]:
    """The transcripts, main and subagents', whose directory may hold sessions of ``places``, changed at or after
    ``since``. The directory's name only picks: the session's own directory decides (``session_questions``)."""
    prefixes = {slug(place) for place in places}
    found = []
    for root in roots:
        try:
            directories = sorted(entry for entry in Path(root).iterdir() if entry.is_dir())
        except OSError:
            continue
        for directory in directories:
            if not any(directory.name.startswith(prefix) for prefix in prefixes):
                continue
            for path in sorted([*directory.glob("*.jsonl"), *directory.glob("*/subagents/**/*.jsonl")]):
                try:
                    if path.stat().st_mtime >= since:
                        found.append(path)
                except OSError:
                    continue
    return found


# -- the questions -------------------------------------------------------------------------------------------------


@dataclass
class SessionQuestions:
    """The questions, and what was read and left out on the way."""

    questions: list = field(default_factory=list)
    transcripts: int = 0
    searches: int = 0  # searches of a source repo
    no_identifier: int = 0  # of them, left out: no term looks like code
    unanswered: int = 0  # left out: no file of the repo opened after

    def per_repo(self) -> list[tuple[str, int]]:
        return sorted(Counter(q["repo"] for q in self.questions).items(), key=lambda item: (-item[1], item[0]))


def question_id(repo: str, text: str) -> str:
    """The id of the question that searched ``text`` in ``repo``: a hash of both, whatever the case of the terms."""
    return ID_PREFIX + hashlib.sha256(f"{repo}\n{text.lower()}".encode()).hexdigest()[:12]


@dataclass
class _Asked:
    repo: str
    terms: list
    searches: int = 0
    files: Counter = field(default_factory=Counter)
    order: dict = field(default_factory=dict)

    def question(self, project: str) -> dict:
        text = " ".join(self.terms)
        files = sorted(self.files, key=lambda path: (-self.files[path], self.order[path]))[:MAX_ANSWERS]
        return {
            "id": question_id(self.repo, text),
            "question": text,
            "type": SESSION_TYPE,
            "project": project,
            "repo": self.repo,
            "files": [f"{self.repo}:{path}" for path in files],
            "searches": self.searches,
        }


def _answers(calls: list[Call], index: int, repo: str, repos: RepoMap, known) -> list[str]:
    """The files of ``repo`` the WINDOW calls after call ``index`` opened, in order, each once."""
    found: list[str] = []
    for later in calls[index + 1 : index + 1 + WINDOW]:
        for hit_repo, path in opened(later, repos):
            if hit_repo == repo and path not in found and answerable(path, known):
                found.append(path)
    return found


def _ask(calls: list[Call], repos: RepoMap, known: dict, out: SessionQuestions) -> Iterator[tuple[str, list, list]]:
    """(repo, terms, answers) of each search of a source repo in one transcript that makes a question."""
    for index, call in enumerate(calls):
        for repo, terms in searched(call, repos).items():
            out.searches += 1
            terms = terms[:MAX_TERMS]
            if not any(code_shaped(term) for term in terms):
                out.no_identifier += 1
                continue
            answers = _answers(calls, index, repo, repos, known.get(repo))
            if not answers:
                out.unanswered += 1
                continue
            yield repo, terms, answers


def session_questions(
    project, roots: Iterable[Path], *, since: float, checkouts: dict[str, Path] | None = None
) -> SessionQuestions:
    """The questions of ``project``'s sessions with tool calls since ``since`` (seconds of the epoch), read from the
    Claude Code projects directories ``roots``; ``checkouts`` gives a repo's checkout instead of harness.yaml."""
    sources = source_repos(project, checkouts)
    repos = repo_map({name: checkout for name, (checkout, _) in sources.items()})
    known = {name: tracked_files(checkout, refs) for name, (checkout, refs) in sources.items()}
    places = harness_places(project.harness.root)
    out, asked = SessionQuestions(), {}
    for path in transcripts(roots, places, since):
        cwd, calls = read_calls(path, since)
        if not _inside(cwd, places):
            continue
        out.transcripts += 1
        for repo, terms, answers in _ask(calls, repos, known, out):
            entry = asked.setdefault((repo, " ".join(terms).lower()), _Asked(repo, terms))
            entry.searches += 1
            for path_in_repo in answers:
                entry.files[path_in_repo] += 1
                entry.order.setdefault(path_in_repo, len(entry.order))
    questions = [entry.question(project.name) for entry in asked.values()]
    out.questions = sorted(questions, key=lambda q: (q["repo"], q["question"].lower(), q["id"]))
    return out
