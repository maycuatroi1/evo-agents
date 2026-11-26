"""Thin wrappers over the git CLI. Reading committed objects keeps connector output a pure function of
the ref, independent of whatever is half-edited in the working tree."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from evo_agents.infra import git as infra_git

FETCH_TIMEOUT = 120.0
COMMIT_START = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})? ")  # %H and the space before %P, as a history record opens
ISO_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})([+-])(\d+):(\d{2})$")


class GitError(infra_git.GitError, RuntimeError):
    pass


class Clone(infra_git.Git):
    """Git in a clone of a source, waiting as long as git takes. Output is read as UTF-8 with bad bytes replaced: a
    history holds commits in Latin-1 (sympy), which must not stop a sync. GitError naming the command and the clone
    when git fails; the OSError itself when git cannot start."""

    def __init__(self, repo: Path):
        super().__init__(repo)

    def fail(self, error: infra_git.GitError) -> BaseException:
        if isinstance(error, infra_git.GitFailed):
            said = error.stderr.decode("utf-8", "replace").strip()
            return GitError(f"git {' '.join(error.words)} failed in {self.directory}: {said}")
        return error.cause if isinstance(error, infra_git.GitUnavailable) else error

    def text(self, *args: str) -> str:
        return self.run(*args, binary=True).stdout.decode("utf-8", "replace")


def is_repo(path: Path) -> bool:
    try:
        Clone(path).run("rev-parse", "--git-dir", binary=True)
        return True
    except (GitError, FileNotFoundError, NotADirectoryError):
        return False


def fetch(repo: Path, remote: str, branch: str, timeout: float = FETCH_TIMEOUT) -> None:
    """Update ``<remote>/<branch>`` from the remote, never prompting for credentials."""
    refspec = f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"
    try:
        result = infra_git.Git(repo, timeout=timeout, env=infra_git.NO_PROMPT).run(
            "fetch", "--quiet", "--no-tags", remote, refspec, check=False
        )
    except infra_git.GitTimeout as exc:
        raise GitError(f"git fetch {remote} {branch} timed out after {timeout:g}s in {repo}") from exc
    except infra_git.GitUnavailable as exc:
        raise exc.cause from None
    if result.returncode != 0:
        raise GitError(f"git fetch {remote} {branch} failed in {repo}: {result.stderr.strip()}")


def remote_url(repo: Path, remote: str = "origin") -> str:
    return Clone(repo).text("remote", "get-url", remote).strip()


def resolve(repo: Path, ref: str) -> str:
    return Clone(repo).text("rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def ls_tree(repo: Path, commit: str) -> list[tuple[str, str]]:
    """(blob sha, path) for every file at a commit."""
    out = Clone(repo).text("ls-tree", "-r", "-z", "--full-tree", commit)
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
    unique = list(dict.fromkeys(shas))
    batch = "".join(f"{s}\n" for s in unique).encode()
    out = Clone(repo).run("cat-file", "--batch", input=batch, binary=True, check=False).stdout
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
    out = Clone(repo).text("log", "--format=%x00%cI", "--name-only", "--no-renames", commit)
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
    for piece in Clone(repo).text(*args).split("\x01")[1:]:
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
    result = Clone(repo).run("merge-base", "--is-ancestor", older, newer, check=False, binary=True)
    if result.returncode not in (0, 1):
        said = result.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git merge-base --is-ancestor {older} {newer} failed in {repo}: {said}")
    return result.returncode == 0


def commit_time(repo: Path, commit: str) -> str:
    return iso_time(Clone(repo).text("show", "-s", "--format=%cI", commit))


def remote_head(repo: Path, remote: str = "origin") -> str | None:
    """The default branch of ``remote`` as the clone recorded it (``refs/remotes/<remote>/HEAD``), or None."""
    try:
        name = Clone(repo).text("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD").strip()
    except GitError:
        return None
    prefix = f"{remote}/"
    return name[len(prefix) :] if name.startswith(prefix) and len(name) > len(prefix) else None


def behind(repo: Path, commit: str, base: str) -> int | None:
    """How many commits ``base`` (a remote-tracking branch such as ``origin/main``) has that ``commit`` lacks, as the
    clone has them now: no fetch. None when the clone has no such branch or either commit is missing."""
    try:
        return int(Clone(repo).text("rev-list", "--count", f"{commit}..refs/remotes/{base}").strip())
    except (GitError, ValueError, OSError):
        return None


def tracked_clean(repo: Path) -> set[str]:
    """Tracked paths whose working-tree content equals HEAD."""
    changed = set(Clone(repo).text("diff", "--name-only", "HEAD").splitlines())
    tracked = set(Clone(repo).text("ls-files").splitlines())
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
