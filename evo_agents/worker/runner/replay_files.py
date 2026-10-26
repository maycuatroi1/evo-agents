"""The git of a replay run (``evo_agents.worker.runner.replay``): the base commit of its task, the files of the change
each arm runs with, and the repository of each arm.

Every command runs with no hook and no fsmonitor of the repository (``gitops.without_hooks``). Each arm gets a
repository of its own, not a worktree of the checkout: ``standalone`` makes a new repository and fetches the base
commit into it from the checkout, so it holds the history up to that commit and nothing after it (protocol v2 fetches a
commit by its name). The files of the change come out of the checkout's objects (``write_at``), as the arm's commit
has them; a path that commit lacks is removed, and a file is never written through a symbolic link.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

from evo_agents.hub import replay
from evo_agents.worker import gitops

GIT_TIMEOUT = 600.0  # a fetch of the whole history of a repository into the arm's
AUTHOR = ("-c", "user.name=evo-agents replay", "-c", "user.email=replay@evo-agents.invalid")


async def _git(cwd: Path, *args: str, env: Mapping[str, str], check: bool = True) -> tuple[int, bytes]:
    """(exit code, stdout as bytes) of ``git -C cwd ARGS``; GitError on a failure when ``check``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**env, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise gitops.GitError(f"git {args[0]} gave no answer within {GIT_TIMEOUT:g}s") from None
    if check and proc.returncode != 0:
        said = " ".join(err.decode(errors="replace").split())[-500:]
        raise gitops.GitError(f"git {args[0]} failed: {said}")
    return proc.returncode, out


async def commit_of(checkout: Path, name: str, env: Mapping[str, str]) -> str | None:
    """The full name of commit ``name`` in ``checkout``, or None when it has no such commit."""
    code, out = await _git(checkout, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}", env=env, check=False)
    found = out.decode().strip()
    return found if code == 0 and replay.SHA.match(found) else None


async def base_commit(checkout: Path, commits: Iterable[str], env: Mapping[str, str]) -> str | None:
    """The base commit of a task whose step made ``commits`` (``replay.base_of``): the first parent of the oldest of
    those the checkout has; None when it has none of them, or the oldest has no parent."""
    found = []
    for name in commits:
        full = await commit_of(checkout, name, env)
        if full is None:
            continue
        _, out = await _git(checkout, "log", "-1", "--format=%P%n%ct", full, env=env)
        parents, _, time = out.decode().partition("\n")
        found.append((full, parents.split(), int(time.strip() or 0)))
    return replay.base_of(found)


async def harness_of(checkout: Path, default: str, head: str, env: Mapping[str, str]) -> list[str]:
    """The harness paths (``replay.harness_path``) the change at ``head`` touches since its merge base with
    ``default``."""
    _, out = await _git(checkout, "merge-base", default, head, env=env)
    base = out.decode().strip()
    _, out = await _git(checkout, "diff", "-z", "--name-only", "--no-renames", base, head, env=env)
    return replay.harness_paths(path for path in out.decode(errors="surrogateescape").split("\0") if path)


async def files_under(checkout: Path, ref: str, directory: str, env: Mapping[str, str]) -> list[str]:
    """The paths of the files under ``directory`` at commit ``ref``."""
    _, out = await _git(checkout, "ls-tree", "-r", "-z", "--name-only", ref, "--", directory, env=env)
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
        _, out = await _git(checkout, "ls-tree", "-z", ref, "--", source, env=env)
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
        _, data = await _git(checkout, "cat-file", "blob", sha, env=env)
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
    await _git(path, "init", "--quiet", env=env)
    await _git(path, "fetch", "--quiet", "--no-tags", str(checkout.resolve()), base, env=env)
    await _git(path, "checkout", "--quiet", "--detach", base, env=env)


async def commit_all(path: Path, message: str, env: Mapping[str, str]) -> str:
    """Commit what the work tree at ``path`` changed, when it changed anything; the commit HEAD is at then."""
    await _git(path, "add", "--all", env=env)
    code, _ = await _git(path, "diff", "--cached", "--quiet", env=env, check=False)
    if code != 0:
        await _git(path, *AUTHOR, "commit", "--quiet", "--no-verify", "-m", message, env=env)
    _, out = await _git(path, "rev-parse", "HEAD", env=env)
    return out.decode().strip()


# The git of a probe run (``evo_agents.worker.runner.probe``), which lays its arms out with the functions above


def _names(out: bytes) -> list[str]:
    return [path for path in out.decode(errors="surrogateescape").split("\0") if path]


async def changed_between(checkout: Path, old: str, new: str, env: Mapping[str, str]) -> list[str]:
    """Every path the commits from ``old`` to ``new`` of ``checkout`` touch."""
    _, out = await _git(checkout, "diff", "-z", "--name-only", "--no-renames", old, new, env=env)
    return _names(out)


async def merge_base(checkout: Path, one: str, other: str, env: Mapping[str, str]) -> str | None:
    code, out = await _git(checkout, "merge-base", one, other, env=env, check=False)
    return out.decode().strip() or None if code == 0 else None


async def first_parent_before(checkout: Path, ref: str, when: str, env: Mapping[str, str]) -> str | None:
    """The commit of ``ref``'s first-parent line that was its tip at ``when`` (ISO 8601): the newest one made before."""
    code, out = await _git(checkout, "rev-list", "-1", "--first-parent", f"--before={when}", ref, env=env, check=False)
    return out.decode().strip() or None if code == 0 else None


async def without_diff(
    checkout: Path, tip: str, old: str, new: str, paths: list[str], scratch: Path, env: Mapping[str, str]
) -> str:
    """A commit of ``checkout`` whose tree is ``tip``'s less the diff of ``paths`` from ``old`` to ``new``, applied in
    reverse in an index of its own under ``scratch``: neither the checkout's work tree nor its index is touched, and
    no ref names the commit. GitError, with git's words, when the diff no longer comes off ``tip``."""
    scratch.mkdir(parents=True, exist_ok=True)
    patch, index = scratch / "change.diff", scratch / "index"
    _, diff = await _git(
        checkout, "diff", "--binary", "--no-renames", "--no-ext-diff", "--no-textconv", old, new, "--", *paths, env=env
    )
    patch.write_bytes(diff)
    own = {**env, "GIT_INDEX_FILE": str(index)}
    await _git(checkout, "read-tree", tip, env=own)
    if diff:
        await _git(checkout, "apply", "--cached", "--reverse", "--binary", str(patch.resolve()), env=own)
    _, out = await _git(checkout, "write-tree", env=own)
    tree = out.decode().strip()
    message = "The harness of the default branch's tip, the change's diff taken off"
    _, out = await _git(checkout, *AUTHOR, "commit-tree", tree, "-p", tip, "-m", message, env=own)
    with contextlib.suppress(OSError):
        index.unlink()
    return out.decode().strip()


async def changed_since(path: Path, start: str, env: Mapping[str, str]) -> list[str]:
    """The files of the repository at ``path`` its work tree changed since commit ``start``, committed or not, and
    those it added that no rule ignores; sorted."""
    _, out = await _git(path, "diff", "-z", "--name-only", "--no-renames", start, env=env)
    _, others = await _git(path, "ls-files", "-z", "--others", "--exclude-standard", env=env)
    return sorted({*_names(out), *_names(others)})
