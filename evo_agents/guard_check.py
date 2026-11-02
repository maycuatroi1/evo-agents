"""The guards of the private names on a machine (core plugin guard, ``docs/guard.md``): what a worker's git hooks, its
``gh`` shim and its own pushes, and the evo-hub plugin's hook on a member's machine, check before text leaves for a
guarded repo.

The policy is a file (``evo_agents.private_names.Policy``, as GET /v1/guard/policy answers it) that the environment
names in EVO_GUARD_POLICY; a missing or unreadable file, or one without a term or a target, guards nothing.

- ``outgoing``: the text a push would send, the messages, paths and added lines of the commits the remote has not
  (``git log -p SOURCE --not --remotes=REMOTE``, as ``lease_leaks`` reads them), at most MAX_COMMITS commits.
- ``hook``: ``commit-msg`` checks the message of a commit in a checkout with a guarded remote; ``pre-push`` checks what
  each ref of the push to a guarded URL sends.
- ``gh``: the shim a worker puts before the real ``gh`` on a run's PATH. ``pr``, ``issue`` and ``release`` commands
  that write text (title, body, notes, files of them) and ``api`` calls to ``repos/OWNER/REPO/...`` (fields and
  ``--input``) on a guarded repo are refused when the text holds a name; the rest runs the real ``gh`` as it was.

Standard library only, besides ``evo_agents.private_names``: a git hook starts a Python for each commit and push.
Run as ``python -m evo_agents.guard_check hook NAME ...``, ``... gh ARGS`` or ``... check [--repo URL] [FILE]``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from evo_agents.private_names import Hit, Policy, refusal

ENV = "EVO_GUARD_POLICY"
SHIM_ENV = "EVO_GUARD_SHIM"  # the directory of the gh shim, which the shim leaves out when it looks for the real gh
MAX_COMMITS = 500
MAX_TEXT_BYTES = 20_000_000
MARK = "\x00evo-guard\x00"  # what git writes for MARK_FORMAT: a NUL never starts a line of a diff
MARK_FORMAT = "%x00evo-guard%x00"
GIT_TIMEOUT = 60.0
ZERO = "0" * 40
REFUSED = 1  # the exit status of a refusal


class Refused(Exception):
    """Text bound for a guarded repo holds a private name; the message says where and shows each."""


def load(path: str | os.PathLike | None = None) -> Policy | None:
    """The policy of the file ``path`` (EVO_GUARD_POLICY when None); None when there is none to apply."""
    name = path if path is not None else os.environ.get(ENV)
    if not name:
        return None
    try:
        found = Policy.from_json(json.loads(Path(name).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return found if found.terms and found.targets else None


def _git(cwd: str | os.PathLike, *args: str) -> tuple[int, str]:
    try:
        done = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, done.stdout


def remote_urls(cwd: str | os.PathLike) -> list[str]:
    """The fetch and push URLs of every remote of the checkout at ``cwd``."""
    code, out = _git(cwd, "remote", "-v")
    return (
        list(dict.fromkeys(line.split()[1] for line in out.splitlines() if len(line.split()) >= 2)) if code == 0 else []
    )


def guarded_remote(policy: Policy, cwd: str | os.PathLike) -> str | None:
    """The first remote URL of the checkout that the policy guards, or None."""
    return next((url for url in remote_urls(cwd) if policy.guards(url)), None)


def _clip(text: str) -> str:
    raw = text.encode(errors="replace")
    return text if len(raw) <= MAX_TEXT_BYTES else raw[:MAX_TEXT_BYTES].decode(errors="ignore")


def outgoing(cwd: str | os.PathLike, source: str, *, remote: str | None = None, base: str | None = None) -> str:
    """The messages, paths and added lines of the commits ``source`` would send: those after ``base`` when it is a
    commit, else those no ref of ``remote`` (of any remote when None) has."""
    since = [f"{base}..{source}"] if base and base != ZERO else [source, "--not", f"--remotes={remote or '*'}"]
    args = ["log", "-p", "--no-color", "--no-ext-diff", f"--max-count={MAX_COMMITS}", f"--format={MARK_FORMAT}%H%n%B"]
    code, out = _git(cwd, *args, *since)
    if code != 0:
        return ""
    kept = []
    for chunk in out.split(MARK)[1:]:
        message, diff_start, diff = chunk.partition("\ndiff --git ")
        kept.append(message.partition("\n")[2])  # the sha's line is no text of the commit
        for line in (diff_start + diff).splitlines():
            if line.startswith("+"):  # an added line, and +++ b/<path>
                kept.append(line[1:])
            elif line.startswith(("rename to ", "copy to ")):
                kept.append(line)
    return _clip("\n".join(kept))


def check_text(policy: Policy, text: str, where: str) -> None:
    hits: list[Hit] = policy.matcher().find(text)
    if hits:
        raise Refused(refusal(hits, where))


def check_push(policy: Policy, cwd: str | os.PathLike, url: str, updates: list[tuple[str, str, str, str]]) -> None:
    """Refused when a ref of a push to ``url`` (a guarded one) sends a commit that holds a name; ``updates`` are the
    lines git hands a pre-push hook: local ref, local sha, remote ref, remote sha."""
    if not policy.guards(url):
        return
    for local_ref, local_sha, remote_ref, remote_sha in updates:
        if local_sha == ZERO:  # a branch deleted sends nothing
            continue
        text = outgoing(cwd, local_sha, base=remote_sha if remote_sha != ZERO else None)
        check_text(policy, f"{remote_ref}\n{text}", f"the push of {local_ref} to {url}")


def check_message(policy: Policy, cwd: str | os.PathLike, path: str) -> None:
    """Refused when the commit message in ``path`` holds a name, in a checkout with a guarded remote."""
    url = guarded_remote(policy, cwd)
    if url is None:
        return
    try:
        message = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    text = "\n".join(line for line in message.splitlines() if not line.startswith("#"))
    check_text(policy, text, f"the commit message (the checkout pushes to {url})")


def hook(name: str, args: list[str], stdin: str) -> int:
    """A git hook of a run: 0 to go on, REFUSED with the reason on stderr."""
    policy = load()
    if policy is None:
        return 0
    cwd = os.getcwd()
    try:
        if name == "commit-msg" and args:
            check_message(policy, cwd, args[0])
        elif name == "pre-push" and len(args) >= 2:
            updates = [tuple(line.split()) for line in stdin.splitlines() if len(line.split()) == 4]
            check_push(policy, cwd, args[1], updates)  # type: ignore[arg-type]
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    return 0


# gh

TEXT_FLAGS = {"--title", "-t", "--body", "-b", "--notes", "-n", "--subject"}
FILE_FLAGS = {"--body-file", "-F", "--notes-file"}
REPO_FLAGS = {"--repo", "-R"}
API_VALUED = {"-X", "--method", "-H", "--header", "-f", "-F", "--field", "--raw-field", "--input", "-q", "--jq", "-t"}
API_VALUED |= {"--template", "-p", "--preview", "--hostname", "--cache"}
WRITES = {
    "pr": {"create", "edit", "comment", "review", "merge"},
    "issue": {"create", "edit", "comment"},
    "release": {"create", "edit"},
}


def _value(args: list[str], i: int) -> tuple[str | None, str | None, int]:
    """(flag, value, next index) of the argument at ``i``: ``--flag value``, ``--flag=value`` or ``-Fvalue``."""
    arg = args[i]
    if arg.startswith("--") and "=" in arg:
        flag, value = arg.split("=", 1)
        return flag, value, i + 1
    if arg in TEXT_FLAGS | FILE_FLAGS | REPO_FLAGS | {"-f", "--field", "--raw-field", "--input"}:
        return arg, (args[i + 1] if i + 1 < len(args) else None), i + 2
    if len(arg) > 2 and arg[0] == "-" and arg[1] != "-" and arg[:2] in TEXT_FLAGS | FILE_FLAGS | REPO_FLAGS | {"-f"}:
        return arg[:2], arg[2:], i + 1
    return None, None, i + 1


def _read(name: str, stdin: list[str | None]) -> str:
    if name == "-":
        if stdin[0] is None:
            stdin[0] = sys.stdin.read()
        return stdin[0]
    try:
        return Path(name).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def gh_texts(args: list[str], stdin: list[str | None]) -> tuple[str | None, list[str]] | None:
    """(the repo a ``gh`` command names, if any, the texts it writes) for a command the guard checks; None for any
    other. ``stdin[0]`` keeps what a ``-`` file read from standard input, for the real gh."""
    words = [a for a in args if not a.startswith("-")]
    if not words:
        return None
    group = words[0]
    if group == "api":
        endpoint, i = "", 1
        while i < len(args):  # the first argument that is no flag nor a flag's value
            if args[i] in API_VALUED:
                i += 2
            elif args[i].startswith("-"):
                i += 1
            else:
                endpoint = args[i]
                break
        parts = endpoint.strip("/").split("/")
        repo = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 and parts[0] == "repos" else None
        if repo is None:
            return None
    elif len(words) >= 2 and words[1] in WRITES.get(group, set()):
        repo = None
    else:
        return None
    texts: list[str] = []
    i = 1
    while i < len(args):
        flag, value, i = _value(args, i)
        if flag is None or value is None:
            continue
        if flag in REPO_FLAGS:
            repo = value
        elif flag in TEXT_FLAGS:
            texts.append(value)
        elif flag in FILE_FLAGS and group != "api":
            texts.append(_read(value, stdin))
        elif flag in {"-f", "--field", "--raw-field", "-F"} and group == "api":
            key, _, val = value.partition("=")
            texts.append(_read(val[1:], stdin) if val.startswith("@") else f"{key} {val}")
        elif flag == "--input":
            texts.append(_read(value, stdin))
    return repo, texts


def gh_check(policy: Policy, args: list[str], cwd: str | os.PathLike, stdin: list[str | None]) -> None:
    """Refused when the ``gh`` command writes text that holds a name to a guarded repo."""
    found = gh_texts(args, stdin)
    if found is None:
        return
    repo, texts = found
    repo = repo or os.environ.get("GH_REPO") or None
    url = repo if repo and policy.guards(repo) else (None if repo else guarded_remote(policy, cwd))
    if url is None:
        return
    check_text(policy, "\n".join(texts), f"the text of `gh {' '.join(args[:2])}` for {url}")


def real_gh() -> str | None:
    """The gh after the shim on PATH."""
    shim = os.environ.get(SHIM_ENV)
    shim_real = os.path.realpath(shim) if shim else None
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.realpath(d) != shim_real]
    return shutil.which("gh", path=os.pathsep.join(dirs))


def gh(args: list[str]) -> int:
    """The shim: check, then run the real gh with the same arguments (and the standard input it read, if any)."""
    stdin: list[str | None] = [None]
    policy = load()
    if policy is not None:
        try:
            gh_check(policy, args, os.getcwd(), stdin)
        except Refused as exc:
            print(str(exc), file=sys.stderr)
            return REFUSED
    found = real_gh()
    if found is None:
        print("gh: command not found", file=sys.stderr)
        return 127
    if stdin[0] is None:
        os.execv(found, [found, *args])
    return subprocess.run([found, *args], input=stdin[0], text=True, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["hook"] and len(args) >= 2:
        stdin = sys.stdin.read() if args[1] == "pre-push" else ""
        return hook(args[1], args[2:], stdin)
    if args[:1] == ["gh"]:
        return gh(args[1:])
    if args[:1] == ["check"]:
        rest, repo = args[1:], None
        if rest[:1] == ["--repo"] and len(rest) >= 2:
            repo, rest = rest[1], rest[2:]
        policy = load()
        if policy is None:
            print("no guard policy here", file=sys.stderr)
            return 0
        text = Path(rest[0]).read_text(encoding="utf-8", errors="replace") if rest else sys.stdin.read()
        if repo is not None and not policy.guards(repo):
            return 0
        try:
            check_text(policy, text, "the text")
        except Refused as exc:
            print(str(exc), file=sys.stderr)
            return REFUSED
        return 0
    print(
        "usage: python -m evo_agents.guard_check hook NAME ARGS | gh ARGS | check [--repo URL] [FILE]", file=sys.stderr
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
