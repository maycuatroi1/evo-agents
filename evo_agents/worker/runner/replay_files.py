"""The git of a replay run (``evo_agents.worker.runner.replay``): the base commit of its task, the files of the change
each arm runs with, and the repository of each arm.

Every command runs with no hook and no fsmonitor of the repository (``gitops.without_hooks``). Each arm gets a
repository of its own, not a worktree of the checkout: ``standalone`` makes a new repository and fetches the base
commit into it from the checkout, so it holds the history up to that commit and nothing after it (protocol v2 fetches a
commit by its name). The files of the change come out of the checkout's objects (``write_at``), as the arm's commit
has them; a path that commit lacks is removed, and a file is never written through a symbolic link.

A step's verify was written where the step ran, a developer's machine whose repos sit side by side in a workspace such
as ``~/github``: ``localized`` writes each path of it into a repo of the project as where that repo is for the replay.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from evo_agents.hub import replay
from evo_agents.infra.git import NO_PROMPT, Git, GitError, GitFailed, GitUnavailable
from evo_agents.worker import gitops

GIT_TIMEOUT = 600.0  # a fetch of the whole history of a repository into the arm's
AUTHOR = ("-c", "user.name=evo-agents replay", "-c", "user.email=replay@evo-agents.invalid")
_HOME = r"(?<![A-Za-z0-9._/-])(?:~|\$HOME|\$\{HOME\}|/(?:home|Users)/[A-Za-z0-9._-]+)"  # a developer's home
_FOLDER = r"/[A-Za-z0-9_-][A-Za-z0-9._-]*"  # a folder of the workspace under it, never a hidden one
_SHELL_SAFE = re.compile(r"[A-Za-z0-9._/@%+=:,-]+")  # a path the shell takes as one word, quoted or not


def localized(command: str, places: Mapping[str, Path]) -> str:
    """``command`` with each path into a developer's checkout of a repo of ``places`` (``~/github/<repo>``, under
    ``$HOME`` or a home directory, at most three folders deep) written as where that repo is here, ``places[repo]``, so
    a step's verify runs on what it ran on for the step. A place the shell would split into words is left out."""
    names = sorted((name for name, path in places.items() if _SHELL_SAFE.fullmatch(str(path))), key=len, reverse=True)
    if not names:
        return command
    pattern = re.compile(rf"{_HOME}(?:{_FOLDER}){{0,3}}?/({'|'.join(map(re.escape, names))})(?![A-Za-z0-9._-])")
    return pattern.sub(lambda found: str(places[found.group(1)]), command)


class _ReplayGit(Git):
    """``git -C cwd`` of a replay in ``env``, never prompting and in the C locale; bytes out. gitops.GitError when it
    fails or gives no answer within GIT_TIMEOUT."""

    def __init__(self, cwd: Path, env: Mapping[str, str]):
        super().__init__(cwd, timeout=GIT_TIMEOUT, env={**env, **NO_PROMPT, "LC_ALL": "C"}, inherit=False)

    def fail(self, error: GitError) -> BaseException:
        if isinstance(error, GitFailed):
            stderr = error.stderr
            said = stderr if isinstance(stderr, str) else stderr.decode(errors="replace")
            said = " ".join(said.split())[-500:]
            return gitops.GitError(f"git {error.command} failed: {said}")
        if isinstance(error, GitUnavailable):
            return error.cause
        return gitops.GitError(f"git {error.command} gave no answer within {GIT_TIMEOUT:g}s")

    async def raw(self, *args: str) -> bytes:
        return await self.aout(*args, binary=True)


async def commit_of(checkout: Path, name: str, env: Mapping[str, str]) -> str | None:
    """The full name of commit ``name`` in ``checkout``, or None when it has no such commit."""
    named = await _ReplayGit(checkout, env).arun("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", check=False)
    found = named.stdout.strip()
    return found if named.ok and replay.SHA.match(found) else None


async def base_commit(checkout: Path, commits: Iterable[str], env: Mapping[str, str]) -> str | None:
    """The base commit of a task whose step made ``commits`` (``replay.base_of``): the first parent of the oldest of
    those the checkout has; None when it has none of them, or the oldest has no parent."""
    found = []
    for name in commits:
        full = await commit_of(checkout, name, env)
        if full is None:
            continue
        out = await _ReplayGit(checkout, env).raw("log", "-1", "--format=%P%n%ct", full)
        parents, _, time = out.decode().partition("\n")
        found.append((full, parents.split(), int(time.strip() or 0)))
    return replay.base_of(found)


async def harness_of(checkout: Path, default: str, head: str, env: Mapping[str, str]) -> list[str]:
    """The harness paths (``replay.harness_path``) the change at ``head`` touches since its merge base with
    ``default``."""
    out = await _ReplayGit(checkout, env).raw("merge-base", default, head)
    base = out.decode().strip()
    out = await _ReplayGit(checkout, env).raw("diff", "-z", "--name-only", "--no-renames", base, head)
    return replay.harness_paths(path for path in out.decode(errors="surrogateescape").split("\0") if path)


async def files_under(checkout: Path, ref: str, directory: str, env: Mapping[str, str]) -> list[str]:
    """The paths of the files under ``directory`` at commit ``ref``."""
    out = await _ReplayGit(checkout, env).raw("ls-tree", "-r", "-z", "--name-only", ref, "--", directory)
    return [path for path in out.decode(errors="surrogateescape").split("\0") if path]


def _target(root: Path, path: str) -> Path | None:
    """Where ``path`` goes under ``root``; None when a part of the way is a symbolic link or leaves ``root``."""
    target = root
    for part in Path(path).parts:
        if part in ("", ".", "..") or target.is_symlink():
            return None
        target = target / part
    return None if target.is_symlink() else target


async def write_at(checkout: Path, ref: str, pairs: Iterable[tuple[str, str]], root: Path, env: Mapping[str, str]):
    """Write, for each (path at ``ref``, path under ``root``) of ``pairs``, the file as commit ``ref`` of ``checkout``
    has it, executable when it is there, or remove it when that commit has none; a link or a submodule is left out.
    The paths written."""
    written = []
    for source, dest in pairs:
        target = _target(root, dest)
        if target is None:
            continue
        out = await _ReplayGit(checkout, env).raw("ls-tree", "-z", ref, "--", source)
        entry = out.decode(errors="surrogateescape").split("\0")[0]
        mode, _, rest = entry.partition(" ")
        kind, _, rest = rest.partition(" ")
        sha = rest.partition("\t")[0]
        if not entry:
            with contextlib.suppress(FileNotFoundError, IsADirectoryError):
                target.unlink()
            continue
        if kind != "blob" or mode == "120000":
            continue
        data = await _ReplayGit(checkout, env).raw("cat-file", "blob", sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        os.chmod(target, 0o755 if mode == "100755" else 0o644)
        written.append(dest)
    return written


def write_texts(texts: Mapping[str, str | None], root: Path, executable: str | None = None) -> list[str]:
    """Write, for each path under ``root`` of ``texts``, its text, or remove the file when the text is None: the files
    a plugin's probe arm places (``probes.arms_problem`` checked them). A file under ``executable`` is made executable;
    a path whose way is a symbolic link or leaves ``root`` is left out. The paths written."""
    written = []
    for dest, text in sorted(texts.items()):
        target = _target(root, dest)
        if target is None:
            continue
        if text is None:
            with contextlib.suppress(FileNotFoundError, IsADirectoryError):
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o755 if executable and dest.startswith(executable + "/") else 0o644)
        written.append(dest)
    return written


async def standalone(checkout: Path, base: str, path: Path, env: Mapping[str, str]) -> None:
    """A new repository at ``path`` with the history of ``checkout`` up to commit ``base``, checked out there."""
    path.mkdir(parents=True)
    git = _ReplayGit(path, env)
    await git.arun("init", "--quiet")
    await git.arun("fetch", "--quiet", "--no-tags", str(checkout.resolve()), base)
    await git.arun("checkout", "--quiet", "--detach", base)


async def commit_all(path: Path, message: str, env: Mapping[str, str]) -> str:
    """Commit what the work tree at ``path`` changed, when it changed anything; the commit HEAD is at then."""
    await _ReplayGit(path, env).arun("add", "--all")
    if not (await _ReplayGit(path, env).arun("diff", "--cached", "--quiet", check=False)).ok:
        await _ReplayGit(path, env).arun(*AUTHOR, "commit", "--quiet", "--no-verify", "-m", message)
    out = await _ReplayGit(path, env).raw("rev-parse", "HEAD")
    return out.decode().strip()


# The git of a probe run (``evo_agents.worker.runner.probe``), which lays its arms out with the functions above


def _names(out: bytes) -> list[str]:
    return [path for path in out.decode(errors="surrogateescape").split("\0") if path]


async def changed_between(checkout: Path, old: str, new: str, env: Mapping[str, str]) -> list[str]:
    """Every path the commits from ``old`` to ``new`` of ``checkout`` touch."""
    out = await _ReplayGit(checkout, env).raw("diff", "-z", "--name-only", "--no-renames", old, new)
    return _names(out)


async def merge_base(checkout: Path, one: str, other: str, env: Mapping[str, str]) -> str | None:
    found = await _ReplayGit(checkout, env).arun("merge-base", one, other, check=False)
    return found.stdout.strip() or None if found.ok else None


async def first_parent_before(checkout: Path, ref: str, when: str, env: Mapping[str, str]) -> str | None:
    """The commit of ``ref``'s first-parent line that was its tip at ``when`` (ISO 8601): the newest one made before."""
    found = await _ReplayGit(checkout, env).arun(
        "rev-list", "-1", "--first-parent", f"--before={when}", ref, check=False
    )
    return found.stdout.strip() or None if found.ok else None


async def without_diff(
    checkout: Path, tip: str, old: str, new: str, paths: list[str], scratch: Path, env: Mapping[str, str]
) -> str:
    """A commit of ``checkout`` whose tree is ``tip``'s less the diff of ``paths`` from ``old`` to ``new``, applied in
    reverse in an index of its own under ``scratch``: neither the checkout's work tree nor its index is touched, and
    no ref names the commit. GitError, with git's words, when the diff no longer comes off ``tip``."""
    scratch.mkdir(parents=True, exist_ok=True)
    patch, index = scratch / "change.diff", scratch / "index"
    diff = await _ReplayGit(checkout, env).raw(
        "diff", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", old, new, "--", *paths
    )
    patch.write_bytes(diff)
    own = _ReplayGit(checkout, {**env, "GIT_INDEX_FILE": str(index)})
    await own.arun("read-tree", tip)
    if diff:
        await own.arun("apply", "--cached", "--reverse", "--binary", str(patch.resolve()))
    out = await own.raw("write-tree")
    tree = out.decode().strip()
    message = "The harness of the default branch's tip, the change's diff taken off"
    out = await own.raw(*AUTHOR, "commit-tree", tree, "-p", tip, "-m", message)
    with contextlib.suppress(OSError):
        index.unlink()
    return out.decode().strip()


async def changed_since(path: Path, start: str, env: Mapping[str, str]) -> list[str]:
    """The files of the repository at ``path`` its work tree changed since commit ``start``, committed or not, and
    those it added that no rule ignores; sorted."""
    out = await _ReplayGit(path, env).raw("diff", "-z", "--name-only", "--no-renames", start)
    others = await _ReplayGit(path, env).raw("ls-files", "-z", "--others", "--exclude-standard")
    return sorted({*_names(out), *_names(others)})
