"""The classes of action (plan autonomy-levels, ``evo_agents.hub.autonomy_levels``) of a Bash command an agent runs: the
table of patterns the worker's PreToolUse hook reads (``autonomy_hook``; ``docs/autonomy.md``, The hook of
autonomy on the worker).

Each pattern names a class and what it matched:

- K9, delete or overwrite data for good: a forced push (``--force``, ``-f``, ``--force-with-lease``, a ``+`` refspec),
  the deletion of a remote branch (``--delete``, ``-d``, a ``:branch`` refspec), DROP, TRUNCATE or a DELETE without a
  WHERE given to an SQL client, ``rm -rf`` of a path outside the run's worktree and the system's temporary directory,
  ``terraform destroy`` (``apply -destroy`` too), ``kubectl delete``, and the recursive deletion of a bucket
  (``aws s3 rm --recursive``, ``aws s3 rb --force``, ``gsutil rm -r``, ``gcloud storage rm --recursive``, ``mc rm
  --recursive``, ``rclone purge``).
- K5, merge into a default branch: ``gh pr merge``, ``glab mr merge``, and a push to a default branch (DEFAULT_BRANCHES,
  and those the caller names).
- K6, deploy, publish, tag a release: a push of a tag ``v*`` (or of every tag), ``gh release create``, ``glab release
  create``.
- K8, credentials: ``evo-agents hub secret`` and ``hub account`` other than a read (READS).
- K10, send out on a person's behalf: ``gh issue|pr create`` and ``glab issue|mr create`` with ``--repo``/``-R``
  naming a repo that is not one of the run's.
- K12, access: ``evo-agents hub admin|policy|agent|org|autonomy`` other than a read.

A command is split into its simple commands at ``&&``, ``||``, ``;``, ``|`` and new lines; ``sudo``, ``env``,
variable assignments and the like before a program are skipped, and the script of ``bash -c``, ``sh -c`` or ``zsh
-c`` is read in turn. The SQL patterns read the whole command, heredocs included, once it names an SQL client.

This is a second layer, never the boundary: a command that reaches the same act another way (a script written to a
file first, base64 piped to a shell, ``eval`` of a variable, a subshell the split does not see, an API called with
curl) is not caught. The boundary is what the hub leases the run (K7) and the environments it may reach.

Standard library only.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

DEFAULT_BRANCHES = ("main", "master")
READS = frozenset({"list", "ls", "show", "get", "status", "usage", "whoami", "help"})
CREDENTIAL_GROUPS = frozenset({"secret", "account"})  # evo-agents hub <group> ...: K8
ACCESS_GROUPS = frozenset({"admin", "policy", "agent", "org", "autonomy"})  # K12
EVO_PROGRAMS = frozenset({"evo-agents", "evo-ak"})
SKIPPED = frozenset({"sudo", "env", "command", "nohup", "time", "exec", "builtin", "nice", "doas", "xargs"})
SHELLS = frozenset({"bash", "sh", "zsh", "dash"})
SQL_CLIENTS = re.compile(
    r"\b(psql|pgcli|mysql|mariadb|sqlite3|sqlcmd|clickhouse-client|clickhouse|duckdb|cockroach|mongosh|cqlsh|"
    r"supabase\s+db|evo-agents\s+hub\s+db)\b"
)
SQL_DROP = re.compile(r"\bdrop\s+(table|database|schema|index|view|materialized\s+view|sequence|type)\b", re.I)
SQL_TRUNCATE = re.compile(r"\btruncate\b(\s+table)?\s+[\w.\"]+", re.I)
SQL_DELETE = re.compile(r"\bdelete\s+from\s+[^;]*", re.I)
SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n", ";;", "|&", "(", ")"})
MAX_WHAT = 200


@dataclass(frozen=True)
class Hit:
    """One class a command acts in, and the words that matched (cut to MAX_WHAT characters)."""

    klass: str
    what: str


def _what(words: Iterable[str]) -> str:
    text = " ".join(words)
    return text if len(text) <= MAX_WHAT else text[: MAX_WHAT - 3] + "..."


def _tokens(command: str) -> list[str]:
    """The words and control operators of ``command``; its words split on blanks when it does not parse."""
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError:  # an unbalanced quote
        return command.split()


def simple_commands(command: str) -> list[list[str]]:
    """The simple commands of ``command``, each its words from the program on (see the module)."""
    found: list[list[str]] = []
    current: list[str] = []
    for token in _tokens(command):
        if token in SEPARATORS or set(token) <= set(";&|()"):
            if current:
                found.append(current)
            current = []
            continue
        current.append(token)
    if current:
        found.append(current)
    return [words for words in (_program(words) for words in found) if words]


def _program(words: list[str]) -> list[str]:
    """``words`` from the program on: assignments, ``sudo``, ``env`` and their options left out."""
    index = 0
    while index < len(words):
        word = words[index]
        if "=" in word and not word.startswith("-") and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
            index += 1
        elif word in SKIPPED or (index > 0 and words[index - 1] in SKIPPED and word.startswith("-")):
            index += 1
        else:
            break
    return words[index:]


def _base(word: str) -> str:
    return PurePosixPath(word).name


def classify(
    command: str,
    worktree: str | os.PathLike | None = None,
    *,
    own_repos: Collection[str] = (),
    default_branches: Collection[str] = (),
    depth: int = 0,
) -> list[Hit]:
    """The classes the Bash ``command`` acts in, each once, in the order the command reaches them. ``worktree`` is the
    run's directory (``rm -rf`` inside it is the run's own); ``own_repos`` the run's repos as ``owner/name``, lower
    case; ``default_branches`` the default branches the run's repos have besides DEFAULT_BRANCHES."""
    hits: list[Hit] = []
    branches = {*DEFAULT_BRANCHES, *default_branches}
    for words in simple_commands(command):
        name = _base(words[0])
        if name in SHELLS and depth < 3:
            script = _shell_script(words)
            if script is not None:
                hits += classify(
                    script, worktree, own_repos=own_repos, default_branches=default_branches, depth=depth + 1
                )
            continue
        hits += _of(name, words, worktree, own_repos, branches)
    hits += _sql(command)
    seen: set[str] = set()
    return [hit for hit in hits if not (hit.klass in seen or seen.add(hit.klass))]


def _shell_script(words: list[str]) -> str | None:
    for index, word in enumerate(words[1:], 1):
        if word.startswith("-") and "c" in word.lstrip("-") and not word.startswith("--"):
            return words[index + 1] if index + 1 < len(words) else None
    return None


def _of(name: str, words: list[str], worktree, own_repos: Collection[str], branches: set[str]) -> list[Hit]:
    if name == "git":
        return _git(words, branches)
    if name == "rm":
        return _rm(words, worktree)
    if name in ("terraform", "tofu", "terragrunt"):
        rest = words[1:]
        if "destroy" in rest or ("apply" in rest and "-destroy" in rest):
            return [Hit("K9", _what(words))]
        return []
    if name in ("kubectl", "oc") and "delete" in words[1:3]:
        return [Hit("K9", _what(words))]
    if name in ("gh", "glab"):
        return _forge(words, own_repos)
    if name in EVO_PROGRAMS:
        return _evo(words[1:], words)
    if name.startswith("python") and words[1:3] == ["-m", "evo_agents"]:
        return _evo(words[3:], words)
    return _buckets(name, words)


def _git(words: list[str], branches: set[str]) -> list[Hit]:
    """A push: forced or deleting (K9), to a default branch (K5), of a tag v* (K6)."""
    args = [word for word in words[1:]]
    while args and args[0] in ("-C", "-c"):  # git -C dir / -c key=value before the subcommand
        args = args[2:]
    if not args or args[0] != "push":
        return []
    rest = args[1:]
    flags = [word for word in rest if word.startswith("-")]
    plain = [word for word in rest if not word.startswith("-")]
    refspecs = plain[1:]  # after the remote
    hits: list[Hit] = []
    forced = any(
        flag in ("--force", "-f", "--mirror", "--prune")
        or flag.startswith("--force-with-lease")
        or (flag.startswith("-") and not flag.startswith("--") and "f" in flag)
        for flag in flags
    )
    deleting = any(flag in ("--delete", "-d") for flag in flags) or any(spec.startswith(":") for spec in refspecs)
    if forced or deleting or any(spec.startswith("+") for spec in refspecs):
        hits.append(Hit("K9", _what(words)))
    targets = [spec.split(":", 1)[-1].lstrip("+") for spec in refspecs]
    targets = [target.removeprefix("refs/heads/") for target in targets]
    if any(target in branches for target in targets) and not deleting:
        hits.append(Hit("K5", _what(words)))
    tags = [target.removeprefix("refs/tags/") for target in targets]
    if any(flag in ("--tags", "--follow-tags") for flag in flags) or any(re.match(r"^v\d", tag) for tag in tags):
        hits.append(Hit("K6", _what(words)))
    return hits


def _inside(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def _rm(words: list[str], worktree) -> list[Hit]:
    """``rm`` recursive and forced of a path outside the worktree and the temporary directory (K9)."""
    flags = "".join(word.lstrip("-") for word in words[1:] if word.startswith("-") and not word.startswith("--"))
    long = {word for word in words[1:] if word.startswith("--")}
    recursive = "r" in flags or "R" in flags or "--recursive" in long
    forced = "f" in flags or "--force" in long
    if not (recursive and forced):
        return []
    base = os.path.realpath(os.fspath(worktree)) if worktree is not None else None
    roots = [root for root in (base,) if root]
    roots += sorted({os.path.realpath(tempfile.gettempdir()), "/tmp", "/private/tmp", "/var/tmp"})
    for target in (word for word in words[1:] if not word.startswith("-")):
        if "$" in target or "`" in target or target.startswith("~"):  # not known before it runs: outside
            return [Hit("K9", _what(words))]
        path = target if target.startswith("/") or base is None else os.path.join(base, target)
        if not _inside(os.path.normpath(path), roots):
            return [Hit("K9", _what(words))]
    return []


def _option(words: list[str], *names: str) -> str | None:
    for index, word in enumerate(words):
        for name in names:
            if word == name and index + 1 < len(words):
                return words[index + 1]
            if word.startswith(name + "="):
                return word.split("=", 1)[1]
    return None


def repo_slug(text: str) -> str:
    """``owner/name`` of a ``--repo`` value: a slug, ``host/owner/name`` or a URL."""
    text = text.strip().lower().removesuffix(".git").rstrip("/")
    parts = [part for part in re.split(r"[/:]", text) if part]
    return "/".join(parts[-2:]) if len(parts) >= 2 else text


def _forge(words: list[str], own_repos: Collection[str]) -> list[Hit]:
    rest = words[1:]
    if rest[:2] in (["pr", "merge"], ["mr", "merge"]):
        return [Hit("K5", _what(words))]
    if rest[:2] == ["release", "create"]:
        return [Hit("K6", _what(words))]
    if rest[:2] in (["issue", "create"], ["pr", "create"], ["mr", "create"]):
        repo = _option(rest, "--repo", "-R")
        if repo is not None and repo_slug(repo) not in {slug.lower() for slug in own_repos}:
            return [Hit("K10", _what(words))]
    return []


def _evo(args: list[str], words: list[str]) -> list[Hit]:
    """``evo-agents hub <group> <verb>``: a write of credentials (K8) or of access (K12)."""
    args = [word for word in args if not word.startswith("-")]
    if len(args) < 2 or args[0] != "hub":
        return []
    group = args[1]
    verb = args[2] if len(args) > 2 else ""
    if verb in READS or not verb:
        return []
    if group in CREDENTIAL_GROUPS:
        return [Hit("K8", _what(words))]
    if group in ACCESS_GROUPS:
        return [Hit("K12", _what(words))]
    return []


def _buckets(name: str, words: list[str]) -> list[Hit]:
    """The recursive deletion of a bucket or of what it holds (K9)."""
    rest = words[1:]
    recursive = any(word in ("-r", "-R", "--recursive") for word in rest)
    if name == "aws" and rest[:2] == ["s3", "rm"] and recursive:
        return [Hit("K9", _what(words))]
    if name == "aws" and rest[:2] == ["s3", "rb"] and "--force" in rest:
        return [Hit("K9", _what(words))]
    if name == "gsutil" and "rm" in rest and (recursive or any(re.match(r"^-\w*r", word) for word in rest)):
        return [Hit("K9", _what(words))]
    if name == "gcloud" and rest[:2] == ["storage", "rm"] and recursive:
        return [Hit("K9", _what(words))]
    if name == "gcloud" and rest[:3] == ["storage", "buckets", "delete"]:
        return [Hit("K9", _what(words))]
    if name == "mc" and rest[:1] in (["rm"], ["rb"]) and (recursive or "--force" in rest):
        return [Hit("K9", _what(words))]
    if name == "rclone" and rest[:1] == ["purge"]:
        return [Hit("K9", _what(words))]
    return []


def _sql(command: str) -> list[Hit]:
    """DROP, TRUNCATE, or a DELETE without WHERE given to an SQL client (K9)."""
    if not SQL_CLIENTS.search(command):
        return []
    for pattern in (SQL_DROP, SQL_TRUNCATE):
        found = pattern.search(command)
        if found:
            return [Hit("K9", _what([found.group(0)]))]
    for found in SQL_DELETE.finditer(command):
        if not re.search(r"\bwhere\b", found.group(0), re.I):
            return [Hit("K9", _what([found.group(0).strip()]))]
    return []
