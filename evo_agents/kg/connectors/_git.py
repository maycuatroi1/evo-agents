"""Thin wrappers over the git CLI. Reading committed objects keeps connector output a pure function of
the ref, independent of whatever is half-edited in the working tree."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

FETCH_TIMEOUT = 120.0
COMMIT_START = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})? ")  # %H and the space before %P, as a history record opens
ISO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-])(\d+):(\d{2})$")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, binary: bool = False):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=not binary)
    if result.returncode != 0:
        err = result.stderr if not binary else result.stderr.decode("utf-8", "replace")
        raise GitError(f"git {' '.join(args)} failed in {repo}: {err.strip()}")
    return result.stdout


def is_repo(path: Path) -> bool:
    try:
        git(path, "rev-parse", "--git-dir")
        return True
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def fetch(repo: Path, remote: str, branch: str, timeout: float = FETCH_TIMEOUT) -> None:
    """Update ``<remote>/<branch>`` from the remote, never prompting for credentials."""
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--quiet", "--no-tags", remote, refspec],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git fetch {remote} {branch} timed out after {timeout:g}s in {repo}") from exc
    if result.returncode != 0:
        raise GitError(f"git fetch {remote} {branch} failed in {repo}: {result.stderr.strip()}")


def resolve(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def ls_tree(repo: Path, commit: str) -> list[tuple[str, str]]:
    """(blob sha, path) for every file at a commit."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
    entries = []
    for record in out.split("\0"):
        if not record:
            continue
        meta, path = record.split("\t", 1)
        _mode, kind, sha = meta.split()
        if kind == "blob":
            entries.append((sha, path))
    return entries


def cat_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    """Read many blobs through one ``git cat-file --batch`` process."""
    if not shas:
        return {}
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    unique = list(dict.fromkeys(shas))
    out, _ = proc.communicate("".join(f"{s}\n" for s in unique).encode())
    blobs: dict[str, bytes] = {}
    pos = 0
    for sha in unique:
        end = out.index(b"\n", pos)
        header = out[pos:end].split()
        pos = end + 1
        if len(header) < 3 or header[1] == b"missing":
            continue
        size = int(header[2])
        blobs[sha] = out[pos : pos + size]
        pos += size + 1
    return blobs


def iso_time(text: str) -> str:
    """A time of ``%cI`` as the protocol takes it. A few old commits hold an offset no clock has (psf/requests has
    one at +518:00), which ``%cI`` prints as is: such a time is moved to UTC, the same instant."""
    text = text.strip()
    m = ISO_TIME.match(text)
    if not m or (len(m[3]) == 2 and int(m[3]) <= 23):
        return text
    offset = timedelta(hours=int(m[3]), minutes=int(m[4]))
    moment = datetime.fromisoformat(m[1]) - (offset if m[2] == "+" else -offset)
    return moment.isoformat() + "Z"


def last_change_times(repo: Path, commit: str) -> dict[str, str]:
    """Committer time of the latest commit touching each path, from one walk of the history."""
    out = git(repo, "log", "--format=%x00%cI", "--name-only", "--no-renames", commit)
    times: dict[str, str] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("\0"):
            current = iso_time(line[1:])
        elif line and current and line not in times:
            times[line] = current
    return times


def history(repo: Path, commit: str, since: str | None = None) -> list[dict]:
    """The commits reachable from ``commit`` and not from ``since``, newest first, each
    ``{sha, parents, time, author_email, message, paths}``: the committer time, the author's email as the commit object
    holds it (``%ae``, never mapped through a ``.mailmap``, which the working tree may hold half-edited; ``""`` when the
    commit names none), the message (``%B``, stripped) and the paths it changed, a merge's against its first parent, a
    rename as a delete and an add. GitError when ``since`` is not a commit of the clone."""
    # \x01 opens a commit, \x02 separates its fields, \x03 ends the message: the paths follow, a line each.
    args = ["-c", "core.quotePath=false", "log", "--format=%x01%H %P%x02%cI%x02%ae%x02%B%x03", "--name-only"]
    args += ["--no-renames", "--diff-merges=first-parent", commit]
    if since:
        args.append(f"^{since}")
    records: list[str] = []
    for piece in git(repo, *args).split("\x01")[1:]:
        if records and not COMMIT_START.match(piece):  # a \x01 a message held (ansible has some), not a new commit
            records[-1] += "\x01" + piece
        else:
            records.append(piece)
    found: list[dict] = []
    for record in records:
        head, _, listed = record.partition("\x03")
        shas, time, email, message = (head.split("\x02", 3) + ["", "", ""])[:4]
        sha, *parents = shas.split()
        paths = [line for line in listed.splitlines() if line]
        found.append(
            {
                "sha": sha,
                "parents": parents,
                "time": iso_time(time),
                "author_email": email.strip(),
                "message": message.strip(),
                "paths": paths,
            }
        )
    return found


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    """Whether ``older`` is ``newer`` or one of its ancestors. GitError when either is not a commit of the clone."""
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", older, newer], capture_output=True, text=True
    )
    if result.returncode not in (0, 1):
        raise GitError(f"git merge-base --is-ancestor {older} {newer} failed in {repo}: {result.stderr.strip()}")
    return result.returncode == 0


def commit_time(repo: Path, commit: str) -> str:
    return iso_time(git(repo, "show", "-s", "--format=%cI", commit))


def remote_head(repo: Path, remote: str = "origin") -> str | None:
    """The default branch of ``remote`` as the clone recorded it (``refs/remotes/<remote>/HEAD``), or None."""
    try:
        name = git(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD").strip()
    except GitError:
        return None
    prefix = f"{remote}/"
    return name[len(prefix) :] if name.startswith(prefix) and len(name) > len(prefix) else None


def behind(repo: Path, commit: str, base: str) -> int | None:
    """How many commits ``base`` (a remote-tracking branch such as ``origin/main``) has that ``commit`` lacks, as the
    clone has them now: no fetch. None when the clone has no such branch or either commit is missing."""
    try:
        return int(git(repo, "rev-list", "--count", f"{commit}..refs/remotes/{base}").strip())
    except (GitError, ValueError, OSError):
        return None


def tracked_clean(repo: Path) -> set[str]:
    """Tracked paths whose working-tree content equals HEAD."""
    changed = set(git(repo, "diff", "--name-only", "HEAD").splitlines())
    tracked = set(git(repo, "ls-files").splitlines())
    return tracked - changed


_REMOTE = re.compile(r"^(?:https?://(?:[^@/]+@)?|git@|ssh://git@)([^/:]+)[:/](.+?)(?:\.git)?/?$")


def remote_key(origin: str) -> tuple[str, str] | None:
    """(host, project) of a remote, the same for its https and ssh spellings."""
    m = _REMOTE.match(origin.strip())
    return (m.group(1).lower(), m.group(2).lower()) if m else None


def web_url(origin: str, branch: str, path: str) -> str | None:
    """Browser URL of a file on its forge, keyed by branch so it does not change with every commit."""
    m = _REMOTE.match(origin.strip())
    if not m:
        return None
    host, project = m.group(1), m.group(2)
    if host == "github.com":
        return f"https://{host}/{project}/blob/{branch}/{path}"
    return f"https://{host}/{project}/-/blob/{branch}/{path}"


def commit_url(origin: str, sha: str) -> str | None:
    """Browser URL of one commit on its forge."""
    m = _REMOTE.match(origin.strip())
    if not m:
        return None
    host, project = m.group(1), m.group(2)
    if host == "github.com":
        return f"https://{host}/{project}/commit/{sha}"
    return f"https://{host}/{project}/-/commit/{sha}"
