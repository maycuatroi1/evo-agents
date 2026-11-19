"""The checks of a whole JavaScript workspace: what the agent of a claude-code plan run may not run while it works.

The full check of a TypeScript monorepo typechecks, lints and tests every package at once: ``pnpm check`` of a root
whose script runs ``turbo run typecheck``, ``turbo run typecheck`` itself, ``pnpm -r typecheck``. With thirty packages
that is three or four ``tsc`` of 1 to 2 GB each. Two plan runs on a worker of 14 GB that serves several runs
(2026-11-18 and 2026-11-19) started one in the background before they pushed: it filled the swap for close to an hour,
memory pressure stalled every run of the machine (sessions timed out at their start, runs sent no event for 50
minutes), and the check ended killed for memory, with no result. The plan run typechecks and tests the packages it
changed; the whole workspace is the CI's.

``whole_workspace`` reads a Bash command and names the first such run in it, in the foreground or the background:

- a run in every package with no filter: ``turbo run TASK`` (``turbo TASK``, ``turbo watch TASK``) without
  ``--filter``, ``-F`` or ``--affected``; ``pnpm -r`` (``--recursive``) of a script, or of ``exec`` of a typecheck,
  test, lint or build tool, without ``--filter``; ``npm run|test|exec`` with ``--workspaces``; ``yarn workspaces
  foreach`` without ``--since``, ``--from`` or ``--include``, and ``yarn workspaces run``; ``nx run-many`` without
  ``--projects``; ``lerna run|exec`` without ``--scope`` or ``--since``; ``tsc --build`` of the root of a workspace.
  A filter that only leaves packages out (``--filter '!web'``) is no filter. Through ``npx``, ``pnpm exec|dlx``,
  ``npm exec``, ``yarn exec|dlx``, ``bunx`` and ``bun x`` too.
- a script of a package.json that runs one, as ``pnpm check``, ``pnpm run typecheck``, ``npm test``, ``yarn check``
  or ``bun run check`` run it. The script is read from the package.json nearest the directory it runs in: the hook's
  cwd, then each ``cd`` of the command before it (a subshell's ends with it), ``pnpm -C DIR``, ``npm --prefix DIR``,
  ``yarn --cwd DIR``, ``bun --cwd DIR``, the workspace's root for ``pnpm -w``. The scripts it calls are read in turn.
  ``pnpm --filter PKG SCRIPT`` and the script of a package that runs none of the above (``cd packages/x && pnpm
  typecheck``) pass.

The bodies of heredocs are text, not commands (``command_classes.without_heredocs``). A word that is a shell variable
is not read, and a script the guard cannot find, or a directory it cannot tell, passes: like ``broad_tests``, this is
a second layer, not a boundary, and the plan run's prompt (``record_rule``) says the same.

Standard library only.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path, PurePosixPath

from evo_agents.worker.command_classes import SHELLS, shell_script, steps

MAX_DEPTH = 6  # scripts, shells and runners read inside one another
MAX_UP = 12  # directories walked up to the package.json of a script
MAX_PACKAGE_BYTES = 1_000_000
MAX_WHAT = 240
TIMEOUTS = frozenset({"timeout", "gtimeout"})
WRAPPERS = frozenset({"nohup", "setsid", "stdbuf", "nice", "ionice", "time"})
WRAPPER_VALUED = frozenset({"-n", "-c", "-s", "-k", "--signal", "--kill-after", "--adjustment", "--class"})
REDIRECT = re.compile(r"^(\d*|&)(>>?|<)$")  # a redirection whose target is the next word
REDIRECTED = re.compile(r"^(\d*|&)(>>?|<)\S")  # one with its target in the same word (2>/dev/null)
ROOT_FILES = ("pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json")
# Tools whose run in every package is a broad check (pnpm -r exec TOOL)
HEAVY = frozenset(
    {"tsc", "tsgo", "vue-tsc", "vitest", "jest", "mocha", "eslint", "playwright", "tsup", "vite", "next", "turbo"}
)
TURBO_OTHER = frozenset(
    {
        "login",
        "logout",
        "link",
        "unlink",
        "prune",
        "gen",
        "generate",
        "daemon",
        "telemetry",
        "ls",
        "query",
        "info",
        "scan",
        "bin",
        "completion",
        "boundaries",
        "devtools",
        "config",
        "docs",
    }
)
PNPM_VALUED = frozenset(
    {
        "-C",
        "--dir",
        "--filter",
        "-F",
        "--filter-prod",
        "--reporter",
        "--workspace-concurrency",
        "--resume-from",
        "--test-pattern",
        "--changed-files-ignore-pattern",
        "--loglevel",
    }
)
PNPM_BUILTINS = frozenset(
    {
        "install",
        "i",
        "add",
        "remove",
        "rm",
        "un",
        "uninstall",
        "update",
        "up",
        "upgrade",
        "list",
        "ls",
        "ll",
        "la",
        "why",
        "outdated",
        "publish",
        "pack",
        "link",
        "ln",
        "unlink",
        "rebuild",
        "rb",
        "prune",
        "store",
        "fetch",
        "audit",
        "licenses",
        "import",
        "init",
        "create",
        "deploy",
        "env",
        "setup",
        "config",
        "c",
        "root",
        "bin",
        "doctor",
        "server",
        "patch",
        "patch-commit",
        "approve-builds",
        "dedupe",
        "cache",
        "help",
        "completion",
        "self-update",
        "install-test",
        "it",
        "view",
        "info",
        "login",
        "logout",
        "whoami",
    }
)
NPM_SCRIPTS = frozenset({"test", "t", "tst", "start", "stop", "restart"})
YARN_BUILTINS = frozenset(
    {
        "install",
        "add",
        "remove",
        "up",
        "upgrade",
        "info",
        "why",
        "init",
        "set",
        "config",
        "cache",
        "constraints",
        "dedupe",
        "explain",
        "npm",
        "pack",
        "patch",
        "plugin",
        "rebuild",
        "stage",
        "link",
        "unlink",
        "version",
        "node",
        "bin",
        "list",
        "outdated",
        "audit",
        "global",
        "login",
        "logout",
        "publish",
    }
)
BUN_BUILTINS = frozenset(
    {"install", "i", "add", "remove", "rm", "update", "outdated", "pm", "link", "unlink", "build", "init", "create",
     "upgrade", "test", "publish", "patch", "audit", "info"}
)  # fmt: skip
REFUSAL = (
    "This plan run would run the checks of every package of a JavaScript workspace ({what}). Typecheck and test the "
    "packages you changed: `pnpm --filter PACKAGE typecheck`, `turbo run typecheck --filter PACKAGE`, or the "
    "package's own script from its directory. The whole workspace (`pnpm check`, `turbo run` without `--filter`, "
    "`pnpm -r`) runs in CI on the pull request: here it takes gigabytes of memory for as long as it runs, in the "
    "foreground or the background, and stalls every run of the worker. When the step's verify names it, run that "
    "once with `evo-agents worker step KEY verify`."
)


def _what(text: str) -> str:
    return text if len(text) <= MAX_WHAT else text[: MAX_WHAT - 3] + "..."


def _base(word: str) -> str:
    return PurePosixPath(word).name


def _bare(words: list[str]) -> list[str]:
    """``words`` from the program on: ``timeout`` with its options and duration, the wrappers of WRAPPERS with their
    options, and redirections left out."""
    index = 0
    while index < len(words):
        name = _base(words[index])
        if name not in TIMEOUTS and name not in WRAPPERS:
            break
        index += 1
        while index < len(words) and words[index].startswith("-"):
            index += 2 if words[index] in WRAPPER_VALUED else 1
        if name in TIMEOUTS:
            index += 1  # the duration
    kept, skip = [], False
    for word in words[index:]:
        if skip:
            skip = False
        elif REDIRECT.match(word):
            skip = True
        elif not REDIRECTED.match(word):
            kept.append(word)
    return kept


def _resolve(here: Path | None, target: str | None) -> Path | None:
    """The directory ``target`` names from ``here``; None when it cannot be told (a variable, ``-``, no ``here``)."""
    if not target or "$" in target or "`" in target or target == "-":
        return None
    if target.startswith("~"):
        target = os.path.expanduser(target)
    if os.path.isabs(target):
        return Path(os.path.normpath(target))
    return Path(os.path.normpath(here / target)) if here is not None else None


def _cd(here: Path | None, args: list[str]) -> Path | None:
    targets = [word for word in args if not word.startswith("-")]
    return _resolve(here, targets[0] if targets else "~")


def _package(here: Path | None) -> tuple[Path, dict] | None:
    """The directory of the package.json nearest ``here`` (itself or above), and what it holds."""
    directory = here
    for _ in range(MAX_UP):
        if directory is None:
            return None
        path = directory / "package.json"
        try:
            if path.is_file() and path.stat().st_size <= MAX_PACKAGE_BYTES:
                data = json.loads(path.read_text(encoding="utf-8"))
                return (directory, data) if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None
        if directory.parent == directory:
            return None
        directory = directory.parent
    return None


def _is_root(directory: Path) -> bool:
    """Whether ``directory`` is the root of a workspace."""
    if any((directory / name).is_file() for name in ROOT_FILES):
        return True
    found = _package(directory)
    return found is not None and found[0] == directory and bool(found[1].get("workspaces"))


def _workspace_root(here: Path | None) -> Path | None:
    directory = here
    for _ in range(MAX_UP):
        if directory is None:
            return None
        if (directory / "pnpm-workspace.yaml").is_file():
            return directory
        if directory.parent == directory:
            return None
        directory = directory.parent
    return None


def _filtered(values: list[str]) -> bool:
    """Whether the values of the filter options of a command narrow it: one that only leaves packages out does not."""
    return any(value and not value.startswith("!") for value in values)


def _options(args: list[str], valued: frozenset[str]) -> tuple[dict[str, list[str]], list[int]]:
    """The options of ``args`` (name to values, ``--name=value`` split) and the indexes of its words that are not
    options; every word after ``--`` is one of those."""
    options: dict[str, list[str]] = {}
    plain: list[int] = []
    index = 0
    while index < len(args):
        word = args[index]
        if word == "--":
            plain += range(index + 1, len(args))
            break
        if word.startswith("-") and len(word) > 1:
            name, equals, value = word.partition("=")
            if equals:
                options.setdefault(name, []).append(value)
            elif name in valued and index + 1 < len(args):
                options.setdefault(name, []).append(args[index + 1])
                index += 1
            else:
                options.setdefault(name, []).append("")
        else:
            plain.append(index)
        index += 1
    return options, plain


def _values(options: dict[str, list[str]], *names: str) -> list[str] | None:
    """The values of the options ``names``; None when none of them is given."""
    found = [value for name in names for value in options.get(name, [])]
    return found if any(name in options for name in names) else None


class _Reader:
    """One reading of a command (``whole_workspace``): the scripts already read, so that one calling itself ends."""

    def __init__(self) -> None:
        self.read: set[tuple[Path, str]] = set()

    def command(self, command: str, here: Path | None, depth: int) -> str | None:
        """What in ``command``, run from ``here``, runs the checks of every package; None when nothing does."""
        if depth > MAX_DEPTH:
            return None
        saved: list[Path | None] = []
        for step in steps(command):
            if step == "(":
                saved.append(here)
                continue
            if step == ")":
                here = saved.pop() if saved else here
                continue
            words = _bare(step) if isinstance(step, list) else []
            if not words:
                continue
            name = _base(words[0])
            if name in ("cd", "pushd"):
                here = _cd(here, words[1:])
                continue
            if name in SHELLS:
                script = shell_script(words)
                found = self.command(script, here, depth + 1) if script else None
            else:
                found = self.run(words, here, depth)
            if found:
                return found
        return None

    def run(self, words: list[str], here: Path | None, depth: int) -> str | None:
        """What in the simple command ``words`` runs the checks of every package; None when nothing does."""
        if depth > MAX_DEPTH or not words:
            return None
        name = _base(words[0])
        args = words[1:]
        if name in ("npx", "pnpx", "bunx"):
            return self._npx(args, here, depth)
        if name == "turbo":
            return _turbo(words)
        if name == "nx" and "run-many" in args[:2]:
            options, _ = _options(args, frozenset({"-p", "--projects", "-t", "--target", "--targets"}))
            return None if _values(options, "-p", "--projects") else _what(" ".join(words))
        if name == "lerna" and args[:1] in (["run"], ["exec"]):
            options, _ = _options(args, frozenset({"--scope", "--since", "--ignore", "--concurrency"}))
            return None if _values(options, "--scope", "--since") is not None else _what(" ".join(words))
        if name in ("tsc", "tsgo", "vue-tsc"):
            return self._tsc(words, here)
        if name == "pnpm":
            return self._pnpm(words, here, depth)
        if name == "npm":
            return self._npm(words, here, depth)
        if name == "yarn":
            return self._yarn(words, here, depth)
        if name == "bun":
            return self._bun(words, here, depth)
        if name in ("run-s", "run-p", "npm-run-all"):
            return self._run_all(words, here, depth)
        return None

    def _npx(self, args: list[str], here: Path | None, depth: int) -> str | None:
        index = 0
        while index < len(args) and args[index].startswith("-"):
            if args[index] in ("-c", "--call") and index + 1 < len(args):
                return self.command(args[index + 1], here, depth + 1)
            index += 2 if args[index] in ("-p", "--package") else 1
        return self.run(args[index:], here, depth + 1)

    def _tsc(self, words: list[str], here: Path | None) -> str | None:
        if "-b" not in words and "--build" not in words:
            return None
        if here is None or not _is_root(here):
            return None
        projects = [word for word in words[1:] if not word.startswith("-")]
        if all(_base(word).startswith("tsconfig") and "/" not in word.strip("./") for word in projects):
            return _what(" ".join(words))
        return None

    def script(self, here: Path | None, name: str, depth: int) -> str | None:
        """What the script ``name`` of the package.json nearest ``here`` runs that checks every package; None when
        it runs nothing such, or there is no such script."""
        found = _package(here)
        if found is None:
            return None
        directory, data = found
        scripts = data.get("scripts")
        body = scripts.get(name) if isinstance(scripts, dict) else None
        if not isinstance(body, str) or (directory, name) in self.read:
            return None
        self.read.add((directory, name))
        inner = self.command(body, directory, depth + 1)
        return f"script {name} runs {inner}" if inner else None

    def _scripted(self, words: list[str], here: Path | None, name: str, depth: int) -> str | None:
        inner = self.script(here, name, depth)
        return _what(f"{' '.join(words)}: {inner}") if inner else None

    def _has_script(self, here: Path | None, name: str) -> bool:
        found = _package(here)
        scripts = found[1].get("scripts") if found else None
        return isinstance(scripts, dict) and name in scripts

    def _pnpm(self, words: list[str], here: Path | None, depth: int) -> str | None:
        args = words[1:]
        options, plain = _options(args, PNPM_VALUED)
        directory = _values(options, "-C", "--dir")
        if directory:
            here = _resolve(here, directory[-1])
        if "-w" in options or "--workspace-root" in options:
            here = _workspace_root(here)
        recursive = "-r" in options or "--recursive" in options
        if plain and args[plain[0]] == "recursive":
            recursive, plain = True, plain[1:]
        filters = _values(options, "--filter", "-F", "--filter-prod") or []
        if not plain or args[plain[0]] in PNPM_BUILTINS:
            return None
        command = args[plain[0]]
        after = args[plain[1] :] if len(plain) > 1 else []  # the script or the tool, and its own arguments
        if command in ("exec", "dlx"):
            if recursive and not _filtered(filters):
                return _what(" ".join(words)) if after and _base(after[0]) in HEAVY else None
            return self.run(after, here, depth + 1)
        if recursive and not _filtered(filters):
            return _what(" ".join(words))
        if filters:
            return None
        if command in ("run", "run-script"):
            return self._scripted(words, here, after[0], depth) if after else None
        if self._has_script(here, command):
            return self._scripted(words, here, command, depth)
        return self.run(args[plain[0] :], here, depth + 1)  # a binary of the workspace (pnpm turbo run ...)

    def _npm(self, words: list[str], here: Path | None, depth: int) -> str | None:
        args = words[1:]
        options, plain = _options(args, frozenset({"--prefix", "-w", "--workspace", "-C"}))
        prefix = _values(options, "--prefix", "-C")
        if prefix:
            here = _resolve(here, prefix[-1])
        every = any(
            values[-1] in ("", "true") for name, values in options.items() if name in ("--workspaces", "--ws", "-ws")
        )
        if not plain:
            return None
        command = args[plain[0]]
        after = args[plain[1] :] if len(plain) > 1 else []
        if command not in ("run", "run-script", "rum", "urn", "exec", "x") and command not in NPM_SCRIPTS:
            return None
        if _values(options, "-w", "--workspace"):
            return None
        if every:
            return _what(" ".join(words))
        if command in ("exec", "x"):
            return self.run(after, here, depth + 1)
        script = command if command in NPM_SCRIPTS else (after[0] if after else None)
        if script is None:
            return None
        return self._scripted(words, here, {"t": "test", "tst": "test"}.get(script, script), depth)

    def _yarn(self, words: list[str], here: Path | None, depth: int) -> str | None:
        args = words[1:]
        options, plain = _options(args, frozenset({"--cwd", "--from", "--include", "--exclude"}))
        directory = _values(options, "--cwd")
        if directory:
            here = _resolve(here, directory[-1])
        if not plain:
            return None
        command = args[plain[0]]
        rest = [args[index] for index in plain[1:]]
        if command == "workspaces":
            if rest[:1] == ["run"]:
                return _what(" ".join(words))
            if rest[:1] == ["foreach"]:
                narrowed = _values(options, "--since", "--from", "--include")
                return None if narrowed is not None else _what(" ".join(words))
            return None
        if command == "workspace" or command in YARN_BUILTINS:
            return None
        if command in ("exec", "dlx"):
            return self.run(args[plain[1] :] if len(plain) > 1 else [], here, depth + 1)
        at = plain[0]
        if command == "run":
            if not rest:
                return None
            command, at = rest[0], plain[1]
        if self._has_script(here, command):
            return self._scripted(words, here, command, depth)
        return self.run(args[at:], here, depth + 1)

    def _bun(self, words: list[str], here: Path | None, depth: int) -> str | None:
        args = words[1:]
        options, plain = _options(args, frozenset({"--cwd", "--filter", "-F"}))
        directory = _values(options, "--cwd")
        if directory:
            here = _resolve(here, directory[-1])
        if not plain or args[plain[0]] in BUN_BUILTINS or _values(options, "--filter", "-F"):
            return None
        command = args[plain[0]]
        if command == "x":
            return self.run(args[plain[1] :] if len(plain) > 1 else [], here, depth + 1)
        if command == "run":
            if len(plain) < 2:
                return None
            command = args[plain[1]]
        return self._scripted(words, here, command, depth) if self._has_script(here, command) else None

    def _run_all(self, words: list[str], here: Path | None, depth: int) -> str | None:
        found = _package(here)
        scripts = found[1].get("scripts") if found else None
        if not isinstance(scripts, dict):
            return None
        for pattern in (word for word in words[1:] if not word.startswith("-")):
            for name in scripts:
                if fnmatch.fnmatchcase(name, pattern):
                    inner = self.script(here, name, depth)
                    if inner:
                        return _what(f"{' '.join(words)}: {inner}")
        return None


def _turbo(words: list[str]) -> str | None:
    """``words`` when ``turbo`` runs tasks in every package (no filter, not ``--affected``); None otherwise."""
    args = words[1:]
    options, plain = _options(args, frozenset({"--filter", "-F"}))
    if _filtered(_values(options, "--filter", "-F") or []) or "--affected" in options:
        return None
    if not plain or args[plain[0]] in TURBO_OTHER:
        return None
    if args[plain[0]] in ("run", "watch") and len(plain) < 2:
        return None
    return _what(" ".join(words))


def whole_workspace(command: str, cwd: str | os.PathLike | None = None) -> str | None:
    """What in the Bash ``command``, run from ``cwd``, runs the checks of every package of a JavaScript workspace (the
    module's rules); None when nothing does."""
    here = Path(os.path.normpath(cwd)) if cwd else None
    return _Reader().command(command, here, 0)
