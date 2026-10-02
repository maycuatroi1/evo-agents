"""Thin wrappers over the git CLI. Reading committed objects keeps connector output a pure function of
the ref, independent of whatever is half-edited in the working tree."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


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


def last_change_times(repo: Path, commit: str) -> dict[str, str]:
    """Committer time of the latest commit touching each path, from one walk of the history."""
    out = git(repo, "log", "--format=%x00%cI", "--name-only", "--no-renames", commit)
    times: dict[str, str] = {}
    current = None
    for line in out.splitlines():
        if line.startswith("\0"):
            current = line[1:]
        elif line and current and line not in times:
            times[line] = current
    return times


def commit_time(repo: Path, commit: str) -> str:
    return git(repo, "show", "-s", "--format=%cI", commit).strip()


def tracked_clean(repo: Path) -> set[str]:
    """Tracked paths whose working-tree content equals HEAD."""
    changed = set(git(repo, "diff", "--name-only", "HEAD").splitlines())
    tracked = set(git(repo, "ls-files").splitlines())
    return tracked - changed


def web_url(origin: str, branch: str, path: str) -> str | None:
    """Browser URL of a file on its forge, keyed by branch so it does not change with every commit."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|git@|ssh://git@)([^/:]+)[:/](.+?)(?:\.git)?/?$", origin.strip())
    if not m:
        return None
    host, project = m.group(1), m.group(2)
    if host == "github.com":
        return f"https://{host}/{project}/blob/{branch}/{path}"
    return f"https://{host}/{project}/-/blob/{branch}/{path}"
