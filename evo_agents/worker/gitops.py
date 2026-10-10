"""The git commands of a run, run as subprocesses of the daemon's event loop.

Every command runs with ``GIT_TERMINAL_PROMPT=0`` (a push that would ask for a password fails instead of waiting)
and ``LC_ALL=C`` (messages the daemon reads are in English), with a timeout, and fails with ``GitError`` carrying the
end of git's own message. Nothing here forces a push, rewrites a commit the remote has, or merges: the one rewrite is
the replay of a plan run's own commits, which the remote's branch lacks, on top of a default branch the remote moved on
(``Replay``, below).

``git``, ``fetch``, ``remote_tip`` and ``push`` take the environment to run git in (``env``): a run passes its own,
whose ``GIT_CONFIG_*`` entries hand its leased origins to its credential helper (``credentials.git_config``); without
one, git gets the environment of this process. A command the remote refused for want of a credential, or for a
credential it does not take, fails with ``GitAuthError``, so the run can take its leases again and try once more
(``credentials.RunCredentials.with_renewal``).

``push`` refuses a repo's default branch (``PushRefused``), except in a plan run whose plan names that very branch for
the repo: there the push goes ahead, never forced, and the answer says it was a default branch with the commits it
added, for the notice ``push_default_branch`` the caller sends the run's owner (``push_notice``). A run of one step
never pushes a default branch. A Builder of the Curator (kind ``curator``) pushes a branch ``curator/...`` alone, and a
default branch never, whatever its plan names; a review run, a judge run and an author run push nothing. ``push``
sends the push options it is given (``--push-option``), with which a push to GitLab opens a merge request.

A branch the remote moved past HEAD (it has HEAD in its history) is left alone: there is nothing to push. A default
branch the plan names that the remote moved on while HEAD has commits of its own (``push`` with a ``Replay``, as
``evo-agents worker step`` and the push at the end of a plan run call it) gets those commits on top of the remote's tip:
they are replayed with ``git rebase`` in a worktree of their own (``replay_commits``), the run's worktree moves to the
result (``reset --keep``, so changes it has that the remote's commits do not touch stay), the verify the replay names
runs again there, and the result is pushed as a fast-forward; a remote that moved on again before the push lands gets
the same, REPLAY_TRIES times in all. Only commits the remote's branch lacks are replayed: never one it has, and never
one the run pushed to it before that the remote no longer has (someone took it out; putting it back is the owner's
call). When the commits do not go on top (a conflict, a merge commit among them, changes of the worktree in the way)
or the verify run again fails, the worktree goes back to HEAD as it was, HEAD is pushed to the replay's side branch
(``evo-run/<run>``, ``SIDE_BRANCH``), the default branch is not touched, and ``push`` raises ``PushConflict``, for the
notice ``conflict_notice`` and the run's failure_cause ``push_conflict``. Any other branch the remote moved on is pushed
as before, and the remote refuses it (``rejected``).

A commit of a run (``commit_run``) holds only the run's own work. It leaves out, at any depth, the paths under
RUN_COMMIT_EXCLUDES (what hooks of the owner's runtime write in a session's directory, such as the learned skills of
``.claude/skills/.learned/``, and the worker's own ``.evo-run/``), and every copy of a hub plan as the hub wrote it
(``evo_agents.hub.mirror.hub_copy``): an export wrote it, not the agent, while a copy the agent edited stays in. Those
paths are put back in the index as HEAD has them, whoever staged them, and named in the answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub.runs import MAX_NOTICE_BODY_BYTES, MAX_NOTICE_COMMITS, RESULT_DIR, clip

TIMEOUT = 120.0
NETWORK_TIMEOUT = 600.0  # fetch and push
MAX_MESSAGE = 2000
PROTECTED = ("main", "master")  # never pushed, whatever the remote's default branch is
SIDE_BRANCH = "evo-run/{id}"  # the remote branch a plan run's commits go to when they do not go on top of its default
REPLAY_TRIES = 3  # replays of a run's commits onto a default branch the remote keeps moving on before the push lands
MAX_CONFLICT_PATHS = 10  # paths of a replay's conflict its message names
# What git prints when the remote refused a push that is no fast-forward: the branch moved on.
REJECTED = re.compile(
    r"\((?:non-fast-forward|fetch first)\)|tip of your current branch is behind"
    r"|behind its remote\W+(?:hint:\W*)?counterpart",
    re.IGNORECASE,
)
EXCLUDE_RESULT = f":(exclude){RESULT_DIR}"  # the agent's result file stays out of every commit
# Directories no commit of a run holds, at any depth: learned skills a Stop hook of the owner's Claude Code writes in
# the session's directory, and the worker's own files beside the agent's work.
RUN_COMMIT_EXCLUDES = (".claude/skills/.learned/", f"{RESULT_DIR}/")
PATHS_PER_CALL = 200  # paths in one `git reset`, well within any argv limit
# What git prints when the remote did not take its credential, or it had none to send (GitHub, GitLab, any http).
AUTH_FAILURE = re.compile(
    r"Authentication failed|could not read (?:Username|Password)|Invalid username or password"
    r"|HTTP Basic: Access denied|returned error: 40[13]\b",
    re.IGNORECASE,
)
_SHORTSTAT = re.compile(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?")


class GitError(Exception):
    pass


class GitAuthError(GitError):
    """The remote refused the command for its credential: none was there, or the remote did not take it."""


class PushRefused(GitError):
    """A push the worker never makes: a default branch, in a run of one step or one its plan does not name."""


class PushConflict(GitError):
    """The remote moved a default branch on, and the run's own commits did not go on top of it, or their verify failed
    there: ``side_branch`` of the remote has them now (``head``, HEAD as it was), and ``branch`` was not touched."""

    def __init__(
        self, message: str, *, branch: str, side_branch: str, head: str, onto: str, commits: tuple[str, ...]
    ) -> None:
        super().__init__(message)
        self.branch = branch  # the default branch the push was for
        self.side_branch = side_branch  # where the commits went instead
        self.head = head  # the commit the side branch has
        self.onto = onto  # the default branch's tip on the remote, which the commits did not go on top of
        self.commits = commits  # the run's commits the default branch lacks, newest first, at most MAX_NOTICE_COMMITS


class Unreplayable(GitError):
    """The run's commits did not replay on top of the remote's tip; the message says why (the paths of a conflict)."""


@dataclass(frozen=True)
class Replay:
    """How ``push`` puts a plan run's own commits on top of a default branch its plan names when the remote moved it on
    (see the module's docstring)."""

    scratch: Path  # where the worktree the commits are replayed in goes, for the replay alone
    side_branch: str  # the remote branch HEAD goes to when the commits do not go on top: SIDE_BRANCH of the run
    pushed: Collection[str] = ()  # commits the run pushed to the branch before; one the remote dropped is not replayed
    # The verify run again in the worktree at the replayed HEAD it is given: why it failed (`x` exited 1), or None.
    verify: Callable[[str], Awaitable[str | None]] | None = None


@dataclass(frozen=True)
class Pushed:
    """What a push did."""

    branch: str  # the remote branch pushed to
    head: str  # HEAD of the worktree, which the branch has now: it points at it, or at a commit after it
    default: bool  # a default branch of the repo, which only a plan run whose plan names it pushes
    changed: bool  # the push moved the branch; False when the branch had ``head`` already, and nothing was sent
    commits: tuple[str, ...] = ()  # the commits it added to the branch, newest first, at most MAX_NOTICE_COMMITS
    onto: str | None = None  # the remote's tip the run's commits were replayed on top of; None without a replay
    replayed_from: str | None = None  # HEAD before the replay, whose commits ``head`` holds again; None without one


@dataclass(frozen=True)
class Workspace:
    """One repo of a plan run on this worker: the owner's checkout, and the worktree the run made of it. The daemon
    keeps it in the run's record (``runs/<run>/run.json``), where ``evo-agents worker step`` reads it."""

    repo: str
    branch: str  # the branch the plan names for the repo: every push goes there
    plan_branch: str | None  # the branch the plan named for the repo when the run was claimed, for check_push
    checkout: Path
    worktree: Path
    local_branch: str  # the branch the worktree is on: ``branch``, or evo-run/<run>/<repo> when that is elsewhere
    base: str  # the commit the worktree started at
    protected: tuple[str, ...]  # the repo's default branches: the remote's HEAD, the hub's default_branch, main, master

    def to_record(self) -> dict:
        return {
            "repo": self.repo,
            "branch": self.branch,
            "plan_branch": self.plan_branch,
            "checkout": str(self.checkout),
            "worktree": str(self.worktree),
            "local_branch": self.local_branch,
            "base": self.base,
            "protected": sorted(self.protected),
        }

    @classmethod
    def from_record(cls, item) -> Workspace:
        """The workspace of a record that ``to_record`` wrote; ValueError for anything else."""
        if not isinstance(item, dict):
            raise ValueError("a repo of a plan run's record is a JSON object")
        try:
            plan_branch = item.get("plan_branch")
            return cls(
                repo=_text(item["repo"]),
                branch=_text(item["branch"]),
                plan_branch=plan_branch if isinstance(plan_branch, str) and plan_branch else None,
                checkout=Path(_text(item["checkout"])),
                worktree=Path(_text(item["worktree"])),
                local_branch=_text(item["local_branch"]),
                base=_text(item["base"]),
                protected=tuple(str(name) for name in item.get("protected") or ()),
            )
        except KeyError as exc:
            raise ValueError(f"a repo of a plan run's record lacks {exc}") from None


def _text(value) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{value!r} is not a name")
    return value


def plan_branches(plan: dict) -> dict[str, str | None]:
    """repo -> the branch the plan's ``repos`` names for it (None when it names none)."""
    found: dict[str, str | None] = {}
    repos = plan.get("repos") if isinstance(plan, dict) else None
    for entry in repos if isinstance(repos, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("repo"), str):
            branch = entry.get("branch")
            found[entry["repo"]] = branch if isinstance(branch, str) and branch else None
    return found


def folder_name(repo: str, taken: Collection[str] = ()) -> str:
    """A name for the worktree of ``repo`` in a plan run's directory, which is also a valid part of a branch name:
    the repo's own name when it is one, and none of ``taken``."""
    base = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "-", repo)).strip(".-") or "repo"
    if base.endswith(".lock"):
        base += "-repo"
    name, number = base, 2
    while name in taken:
        name, number = f"{base}-{number}", number + 1
    return name


def _env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def _tail(text: str, limit: int = MAX_MESSAGE) -> str:
    text = " ".join(text.strip().split())
    return text if len(text) <= limit else "..." + text[-limit:]


async def git(
    cwd: Path, *args: str, timeout: float = TIMEOUT, check: bool = True, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    """(exit code, stdout, stderr) of ``git -C cwd ARGS`` in ``env`` (this process's environment when None); GitError
    on a failure when ``check``."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(env),
        start_new_session=True,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise GitError(f"git {args[0]} gave no answer within {timeout:g}s") from None
    stdout, stderr = out.decode(errors="replace"), err.decode(errors="replace")
    if check and proc.returncode != 0:
        error = GitAuthError if AUTH_FAILURE.search(stderr) else GitError
        raise error(f"git {args[0]} failed ({proc.returncode}): {_tail(stderr or stdout)}")
    return proc.returncode, stdout, stderr


async def rev(cwd: Path, ref: str) -> str | None:
    """The commit ``ref`` names, or None."""
    code, out, _ = await git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    return out.strip() if code == 0 and out.strip() else None


async def has_remote(cwd: Path, name: str = "origin") -> bool:
    code, out, _ = await git(cwd, "remote", check=False)
    return code == 0 and name in out.split()


async def fetch(cwd: Path, remote: str = "origin", *, env: Mapping[str, str] | None = None) -> None:
    await git(cwd, "fetch", "--prune", "--quiet", remote, timeout=NETWORK_TIMEOUT, env=env)


async def remote_urls(cwd: Path, remote: str = "origin") -> list[str]:
    """The URLs of the remote as its configuration writes them (``url``, then ``pushurl``), before any ``insteadOf``
    rewrites them; empty when it has none."""
    found: list[str] = []
    for key in ("url", "pushurl"):
        _, out, _ = await git(cwd, "config", "--get-all", f"remote.{remote}.{key}", check=False)
        found += [line.strip() for line in out.splitlines() if line.strip() and line.strip() not in found]
    return found


async def remote_default_branch(cwd: Path, remote: str = "origin") -> str | None:
    """The branch the remote's HEAD points at, as the last clone or ``remote set-head`` saw it."""
    code, out, _ = await git(cwd, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD", check=False)
    name = out.strip()
    return name.removeprefix(f"{remote}/") if code == 0 and name.startswith(f"{remote}/") else None


async def branches_in_worktrees(cwd: Path) -> set[str]:
    """The branches checked out in any worktree of the repository."""
    _, out, _ = await git(cwd, "worktree", "list", "--porcelain")
    return {
        line.removeprefix("branch refs/heads/") for line in out.splitlines() if line.startswith("branch refs/heads/")
    }


async def is_ancestor(cwd: Path, older: str, newer: str) -> bool:
    code, _, _ = await git(cwd, "merge-base", "--is-ancestor", older, newer, check=False)
    return code == 0


async def check_branch_name(cwd: Path, branch: str) -> bool:
    code, _, _ = await git(cwd, "check-ref-format", "--branch", branch, check=False)
    return code == 0 and not branch.startswith("-")


async def add_worktree(cwd: Path, path: Path, branch: str, start: str, *, reset: bool) -> None:
    """A worktree at ``path`` on ``branch`` at ``start``; ``reset`` moves an existing branch there (-B)."""
    await git(cwd, "worktree", "add", "--no-track", "-B" if reset else "-b", branch, str(path), start)


async def add_detached_worktree(cwd: Path, path: Path, start: str, *, env: Mapping[str, str] | None = None) -> None:
    """A worktree at ``path`` with HEAD detached at ``start``, on no branch: a review run's, which commits nothing."""
    await git(cwd, "worktree", "add", "--detach", str(path), start, env=env)


def without_hooks(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """``base`` (this process's environment when None) with git's configuration that runs no hook and no fsmonitor of
    the repository, whatever its own configuration says: for the git commands of a judge run in the checkout and
    worktree of the change it judges (its diff also takes no textconv or external diff, ``diff_text``)."""
    env = dict(os.environ if base is None else base)
    try:
        start = max(0, int(env.get("GIT_CONFIG_COUNT") or 0))
    except ValueError:
        start = 0
    entries = (("core.hooksPath", os.devnull), ("core.fsmonitor", "false"))
    for offset, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{start + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(start + len(entries))
    return env


async def git_dir(cwd: Path, *, env: Mapping[str, str] | None = None) -> str | None:
    """The absolute git directory of the work tree at ``cwd``, found there and never above it; None without one."""
    found = {**(env if env is not None else os.environ), "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent)}
    code, out, _ = await git(cwd, "rev-parse", "--absolute-git-dir", check=False, env=found)
    return out.strip() if code == 0 and out.strip() else None


async def restore(cwd: Path, head: str, *, expected_git_dir: str, env: Mapping[str, str] | None = None) -> None:
    """Put the work tree at ``cwd`` back at commit ``head``, detached: every tracked file as ``head`` has it, every
    untracked one removed, those a .gitignore or info/exclude hides included (``clean -x``), so nothing a command
    left reaches the next one or the Judge's agent; what a command installs in the work tree (a virtualenv,
    node_modules) goes too, and each command that needs it installs it again. Between two commands of the code a judge
    run judges, which may have removed the work tree's link to its repository or pointed it elsewhere: GitError then,
    before git touches anything, since its git directory is no longer ``expected_git_dir``."""
    local = {**(env if env is not None else os.environ), "GIT_CEILING_DIRECTORIES": str(Path(cwd).parent)}
    found = await git_dir(cwd, env=local)
    if found is None or Path(found).resolve() != Path(expected_git_dir).resolve():
        raise GitError(f"{cwd} is no longer the worktree it was made as: its git directory is {found or 'gone'}")
    await git(cwd, "checkout", "--quiet", "--force", "--detach", head, env=local)
    await git(cwd, "clean", "-ffdxq", env=local)


async def current_branch(cwd: Path) -> str | None:
    """The branch checked out at ``cwd``; None when HEAD is detached."""
    code, out, _ = await git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return out.strip() if code == 0 and out.strip() else None


@dataclass(frozen=True)
class Committed:
    """What ``commit_run`` did."""

    made: bool  # a commit was made
    left_out: tuple[str, ...] = ()  # paths with changes kept out of it, relative to the work tree, sorted


def excluded(path: str) -> bool:
    """Whether ``path`` (relative to the work tree, with /) lies under one of RUN_COMMIT_EXCLUDES, at any depth."""
    where = f"/{path}"
    return any(f"/{prefix}" in where for prefix in RUN_COMMIT_EXCLUDES)


def _left_out(cwd: Path, paths: list[str]) -> list[str]:
    from evo_agents.hub.mirror import hub_copy

    return [path for path in paths if excluded(path) or hub_copy(cwd / path)]


async def staged_paths(cwd: Path) -> list[str]:
    """The paths whose index entry differs from HEAD, each side of a rename on its own."""
    _, out, _ = await git(cwd, "diff", "--cached", "--name-only", "--no-renames", "-z")
    return [path for path in out.split("\0") if path]


async def commit_run(cwd: Path, message: str) -> Committed:
    """Stage every change of the work tree but the result directory, put the paths a run never commits back as HEAD
    has them (see the module's docstring), and commit what is left as ``message``."""
    await git(cwd, "add", "--all", "--", ".", EXCLUDE_RESULT)
    staged = await staged_paths(cwd)
    left = sorted(await asyncio.to_thread(_left_out, cwd, staged))
    for start in range(0, len(left), PATHS_PER_CALL):
        chunk = left[start : start + PATHS_PER_CALL]
        await git(cwd, "reset", "--quiet", "--", *(f":(literal){path}" for path in chunk))
    if len(left) == len(staged):
        return Committed(False, tuple(left))
    await git(cwd, "commit", "--quiet", "-m", message)
    return Committed(True, tuple(left))


async def commit_all(cwd: Path, message: str) -> bool:
    """``commit_run``; whether it made a commit."""
    return (await commit_run(cwd, message)).made


async def diffstat(cwd: Path, base: str, head: str = "HEAD") -> dict:
    _, out, _ = await git(cwd, "diff", "--shortstat", base, head)
    match = _SHORTSTAT.search(out)
    if not match:
        return {"files": 0, "insertions": 0, "deletions": 0}
    files, insertions, deletions = (int(value or 0) for value in match.groups())
    return {"files": files, "insertions": insertions, "deletions": deletions}


async def diff(cwd: Path, base: str, head: str = "HEAD", prefix: str | None = None) -> bytes:
    """The binary diff from ``base`` to ``head``; with ``prefix``, its paths are under that directory (a/<prefix>/...),
    so the diffs of a plan run's repos read as one."""
    where = [f"--src-prefix=a/{prefix}/", f"--dst-prefix=b/{prefix}/"] if prefix else []
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        "diff",
        "--binary",
        *where,
        base,
        head,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=_env(),
    )
    out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    if proc.returncode != 0:
        raise GitError(f"git diff failed ({proc.returncode})")
    return out


def check_push(branch: str, protected: Collection[str], *, kind: str, plan_branch: str | None) -> bool:
    """Whether a push to ``branch`` goes to a default branch of the repo (one of ``protected``): False when it does
    not; True when it does and the run may push it, a plan run (``kind`` plan) whose plan names exactly that branch for
    the repo (``plan_branch``); PushRefused otherwise, so a run of one step never pushes a default branch. A review run,
    a judge run and an author run push nothing at all, and a Builder of the Curator (``kind`` curator) only a branch
    ``curator/...`` that is no default branch."""
    if kind in ("review", "judge", "author"):
        raise PushRefused(f"a {kind} run reads and pushes nothing")
    if kind == "curator":
        from evo_agents.hub.judge import is_curator_branch

        if branch in protected or branch in PROTECTED:
            raise PushRefused(f"{branch} is a default branch: a run of the Curator never pushes it")
        if not is_curator_branch(branch):
            raise PushRefused(f"{branch} is not a branch of the Curator: a run of the Curator pushes curator/... alone")
        return False
    if branch not in protected:
        return False
    if kind == "plan" and plan_branch is not None and branch == plan_branch:
        return True
    if kind == "plan":
        raise PushRefused(
            f"{branch} is a default branch of the repo and the plan does not name it for the repo: the worker pushes "
            "a default branch only when the plan names it"
        )
    raise PushRefused(f"{branch} is a default branch: the worker never pushes it in a run of one step")


async def remote_tip(
    cwd: Path, branch: str, remote: str = "origin", *, env: Mapping[str, str] | None = None
) -> str | None:
    """The commit ``branch`` points at on the remote now, or None when the remote has no such branch."""
    _, out, _ = await git(cwd, "ls-remote", "--heads", remote, f"refs/heads/{branch}", timeout=NETWORK_TIMEOUT, env=env)
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and sha.strip():
            return sha.strip()
    return None


async def has_commit(
    cwd: Path, tip: str, commit: str, branch: str, remote: str = "origin", *, env: Mapping[str, str] | None = None
) -> bool:
    """Whether ``tip``, which ``branch`` of the remote points at, has ``commit`` in its history: ``tip`` is fetched
    first when this repository lacks it. False when git cannot tell."""
    if await rev(cwd, tip) is None:
        with contextlib.suppress(GitError):  # the push that follows says what went wrong
            await git(
                cwd, "fetch", "--quiet", remote, f"refs/heads/{branch}", timeout=NETWORK_TIMEOUT, env=env, check=False
            )
        if await rev(cwd, tip) is None:
            return False
    return await is_ancestor(cwd, commit, tip)


async def new_commits(cwd: Path, before: str | None, remote: str = "origin", limit: int = MAX_NOTICE_COMMITS) -> list:
    """The commits of HEAD that ``before`` lacks (or that no branch of the remote has, when ``before`` is None),
    newest first, at most ``limit``; empty when git cannot tell."""
    since = [f"^{before}"] if before else ["--not", f"--remotes={remote}"]
    code, out, _ = await git(cwd, "rev-list", f"--max-count={limit}", "HEAD", *since, check=False)
    return out.split() if code == 0 else []


def rejected(exc: Exception) -> bool:
    """Whether ``exc`` is a push the remote refused as no fast-forward: its branch moved on."""
    return isinstance(exc, GitError) and bool(REJECTED.search(str(exc)))


async def _send(
    cwd: Path,
    source: str,
    branch: str,
    remote: str,
    *,
    env: Mapping[str, str] | None,
    options: Collection[str] = (),
) -> None:
    """``git push`` of ``source`` (HEAD, or a commit) to ``branch`` of the remote, never forced."""
    sent = [f"--push-option={option}" for option in options]
    await git(
        cwd,
        "push",
        "--quiet",
        "--porcelain",
        *sent,
        remote,
        f"{source}:refs/heads/{branch}",
        timeout=NETWORK_TIMEOUT,
        env=env,
    )


async def push(
    cwd: Path,
    branch: str,
    remote: str = "origin",
    *,
    protected: Collection[str] = (),
    kind: str = "step",
    plan_branch: str | None = None,
    env: Mapping[str, str] | None = None,
    options: Collection[str] = (),
    replay: Replay | None = None,
) -> Pushed:
    """Push HEAD to ``branch`` of the remote, with git in ``env``: a fast-forward or nothing, never forced. A default
    branch of the repo (one of ``protected``) only as ``check_push`` allows, PushRefused before anything is sent
    otherwise. A branch that has HEAD already, pointing at it or at a commit after it, is left alone: a worktree the
    remote moved past since the run began has nothing to push. With ``replay``, a default branch the remote moved on
    gets the run's own commits on top of its tip, or PushConflict once they are on the replay's side branch (see the
    module's docstring)."""
    default = check_push(branch, protected, kind=kind, plan_branch=plan_branch)
    head = await rev(cwd, "HEAD")
    if head is None:
        raise GitError("HEAD names no commit: there is nothing to push")
    if default and replay is not None:
        return await _push_replaying(cwd, branch, remote, head, replay, env=env, options=options)
    before = await remote_tip(cwd, branch, remote, env=env)
    if before == head or (before is not None and await has_commit(cwd, before, head, branch, remote, env=env)):
        return Pushed(branch, head, default, False)
    commits = tuple(await new_commits(cwd, before, remote))
    await _send(cwd, "HEAD", branch, remote, env=env, options=options)
    return Pushed(branch, head, default, True, commits)


async def _push_replaying(
    cwd: Path,
    branch: str,
    remote: str,
    head: str,
    replay: Replay,
    *,
    env: Mapping[str, str] | None,
    options: Collection[str],
) -> Pushed:
    """``push`` of a default branch the plan names, with ``replay``: HEAD as a fast-forward, after its own commits were
    replayed on top of the remote's tip when the remote moved the branch on, as many as REPLAY_TRIES times."""
    original, onto = head, None
    for attempt in range(1, REPLAY_TRIES + 1):
        before = await remote_tip(cwd, branch, remote, env=env)
        if before == head or (before is not None and await has_commit(cwd, before, head, branch, remote, env=env)):
            return Pushed(branch, head, True, False, (), onto, original if head != original else None)
        if before is not None and not await is_ancestor(cwd, before, head):
            if await rev(cwd, before) is None:  # has_commit fetched it; nothing replays onto a commit not here
                raise GitError(f"the tip {before[:12]} of {branch} on {remote} could not be fetched to replay onto")
            head = await _replay(cwd, branch, remote, before, head, original, replay, env=env)
            onto = before
            if head == before:  # every commit of the run was on the remote's branch already, as another commit
                return Pushed(branch, head, True, False, (), onto, original)
        commits = tuple(await new_commits(cwd, before, remote))
        try:
            await _send(cwd, "HEAD", branch, remote, env=env, options=options)
        except GitError as exc:
            if not rejected(exc) or isinstance(exc, GitAuthError):
                raise
            if attempt == REPLAY_TRIES:
                why = f"went on top of it {REPLAY_TRIES} times, and each time it moved on again before the push"
                raise await _conflict(cwd, branch, remote, original, before or head, why, replay, env=env) from None
            continue  # the remote moved the branch on between the look and the push
        return Pushed(branch, head, True, True, commits, onto, original if head != original else None)
    raise AssertionError("unreachable")  # pragma: no cover


async def _replay(
    cwd: Path,
    branch: str,
    remote: str,
    onto: str,
    head: str,
    original: str,
    replay: Replay,
    *,
    env: Mapping[str, str] | None,
) -> str:
    """The commits of ``head`` that ``onto`` lacks, replayed on top of ``onto``, the worktree at ``cwd`` moved there and
    the replay's verify run again in it; the new HEAD. PushConflict, the worktree back at ``original`` and that on the
    side branch, when they do not go on top or their verify fails."""

    async def conflict(why: str) -> PushConflict:
        return await _conflict(cwd, branch, remote, original, onto, why, replay, env=env)

    for sha in replay.pushed:
        if sha and await is_ancestor(cwd, sha, head) and not await is_ancestor(cwd, sha, onto):
            raise await conflict(
                f"include {sha[:12]}, which the run pushed to {branch} before and the remote no longer has: the worker "
                "does not put back a commit the remote dropped"
            )
    _, merges, _ = await git(cwd, "rev-list", "--merges", f"{onto}..{head}", check=False)
    if merges.split():
        raise await conflict(f"hold a merge commit ({merges.split()[0][:12]}), which the worker does not replay")
    try:
        new = await replay_commits(cwd, onto, head, replay.scratch)
    except Unreplayable as exc:
        raise await conflict(f"did not go on top of it ({exc})") from None
    code, out, err = await git(cwd, "reset", "--quiet", "--keep", new, check=False)
    if code != 0:
        why = f"went on top of it, but the worktree has changes the remote's commits touch ({_tail(err or out, 300)})"
        raise await conflict(why)
    if replay.verify is not None:
        failed = await replay.verify(new)
        if failed:
            raise await conflict(f"went on top of it, but the verify run again there failed: {failed}")
    return new


async def replay_commits(cwd: Path, onto: str, head: str, scratch: Path) -> str:
    """The commits of ``head`` that ``onto`` lacks, replayed on top of ``onto`` with ``git rebase`` in a worktree of
    their own at ``scratch``, detached and without the repository's hooks, which is removed after: the commit the
    replay ends on. A commit whose change ``onto`` has already is dropped. Unreplayable when one does not apply, naming
    the paths in conflict; nothing of the repository's branches moves either way."""
    local = {**without_hooks(None), "GIT_EDITOR": "true"}
    await _drop_scratch(cwd, scratch)
    scratch.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        await git(cwd, "worktree", "add", "--quiet", "--detach", str(scratch), head, env=local)
        code, out, err = await git(
            scratch,
            "-c",
            "rebase.updateRefs=false",
            "-c",
            "rebase.autoStash=false",
            "rebase",
            "--quiet",
            "--no-autosquash",
            onto,
            check=False,
            env=local,
        )
        if code != 0:
            _, unmerged, _ = await git(scratch, "diff", "--name-only", "--diff-filter=U", check=False, env=local)
            await git(scratch, "rebase", "--abort", check=False, env=local)
            paths = unmerged.split()
            if paths:
                more = f" and {len(paths) - MAX_CONFLICT_PATHS} more" if len(paths) > MAX_CONFLICT_PATHS else ""
                raise Unreplayable(f"conflict in {', '.join(paths[:MAX_CONFLICT_PATHS])}{more}")
            raise Unreplayable(_tail(err or out, 300) or f"git rebase exited {code}")
        new = await rev(scratch, "HEAD")
        if new is None:
            raise Unreplayable("the replay ended on no commit")
        return new
    finally:
        await _drop_scratch(cwd, scratch)


async def _drop_scratch(cwd: Path, scratch: Path) -> None:
    await git(cwd, "worktree", "remove", "--force", str(scratch), check=False)
    if scratch.exists():
        await asyncio.to_thread(shutil.rmtree, scratch, True)
    await prune_worktrees(cwd)


async def _conflict(
    cwd: Path,
    branch: str,
    remote: str,
    original: str,
    onto: str,
    why: str,
    replay: Replay,
    *,
    env: Mapping[str, str] | None,
) -> PushConflict:
    """Put the worktree back at ``original`` and push it to the replay's side branch, never forced (a side branch
    that has it already is left alone); the PushConflict that says so. A push to the side branch that fails raises."""
    if await rev(cwd, "HEAD") != original:
        await git(cwd, "reset", "--quiet", "--keep", original, check=False)
    side = replay.side_branch
    tip = await remote_tip(cwd, side, remote, env=env)
    if tip != original and not (tip is not None and await has_commit(cwd, tip, original, side, remote, env=env)):
        await _send(cwd, original, side, remote, env=env)
    _, out, _ = await git(cwd, "rev-list", f"--max-count={MAX_NOTICE_COMMITS}", original, f"^{onto}", check=False)
    commits = tuple(out.split())
    message = (
        f"{branch} of {remote} moved on to {onto[:12]}, and the run's commits {why}. They are on {side} of {remote} "
        f"now, at {original[:12]} ({len(commits)} commit(s) {branch} lacks); {branch} was not touched"
    )
    return PushConflict(message, branch=branch, side_branch=side, head=original, onto=onto, commits=commits)


def push_notice(run_id: int, repo: str, pushed: Pushed) -> dict:
    """The body of the notice ``push_default_branch`` for a push to a default branch, as
    POST /v1/worker/runs/{id}/notices takes it."""
    count = len(pushed.commits)
    many = "" if count < MAX_NOTICE_COMMITS else " or more"
    title = f"Run #{run_id} pushed {count}{many} commit(s) to {pushed.branch} of {repo}"
    lines = [
        f"Plan run #{run_id} pushed {repo} to {pushed.branch}, a default branch the plan names for it, never forced.",
        f"{pushed.branch} is now at {pushed.head}.",
    ]
    if pushed.onto:
        lines.append(
            f"The remote had moved {pushed.branch} on to {pushed.onto}: the run's own commits were put on top of it."
        )
    if pushed.commits:
        lines += ["", "Commits, newest first:", *(f"- {sha}" for sha in pushed.commits)]
    return {
        "kind": "push_default_branch",
        "title": " ".join(title.split())[:200],
        "body": clip("\n".join(lines), MAX_NOTICE_BODY_BYTES),
        "repo": repo,
        "branch": pushed.branch,
        "commits": list(pushed.commits),
    }


def conflict_notice(run_id: int, repo: str, conflict: PushConflict) -> dict:
    """The body of the notice ``run_failed`` a plan run's worker sends when its commits went to the side branch instead
    of a default branch the remote moved on (PushConflict), as POST /v1/worker/runs/{id}/notices takes it."""
    side, branch = conflict.side_branch, conflict.branch
    title = f"Run #{run_id} could not push {branch} of {repo}: its commits are on {side}"
    lines = [
        f"Plan run #{run_id} did not push {repo} to {branch}: {conflict}.",
        "",
        f"The run ends failed (push_conflict). Merge or rebase {side} into {branch} by hand, then go on with the plan.",
    ]
    if conflict.commits:
        lines += [
            "",
            f"Commits on {side} that {branch} lacks, newest first:",
            *(f"- {sha}" for sha in conflict.commits),
        ]
    return {
        "kind": "run_failed",
        "title": " ".join(title.split())[:200],
        "body": clip("\n".join(lines), MAX_NOTICE_BODY_BYTES),
        "repo": repo,
        "branch": side,
        "commits": list(conflict.commits),
    }


async def changed_paths(cwd: Path, base: str) -> list[str]:
    """Every path the work tree at ``cwd`` changed since ``base``: committed, staged, changed and untracked, both
    sides of a rename, the result directory left out; sorted."""
    _, committed, _ = await git(cwd, "diff", "--name-only", "--no-renames", "-z", base, "HEAD", check=False)
    _, status, _ = await git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    found = {path for path in committed.split("\0") if path}
    for entry in status.split("\0"):
        if len(entry) > 3:
            found.add(entry[3:])
    return sorted(path for path in found if not path.startswith(f"{RESULT_DIR}/"))


async def merge_base(cwd: Path, first: str, second: str) -> str | None:
    code, out, _ = await git(cwd, "merge-base", first, second, check=False)
    return out.strip() if code == 0 and out.strip() else None


DIFF_TEXT_LIMIT = 8 * 1024 * 1024  # the most of a judged diff read; a longer one is not judged in part


async def diff_text(
    cwd: Path, base: str, head: str, limit: int | None = None, *, env: Mapping[str, str] | None = None
) -> tuple[str, bool]:
    """(the text diff from ``base`` to ``head`` as ``git diff`` writes it, binary files named and no textconv of the
    repository applied, at most ``limit`` characters; whether it was longer and so was cut)."""
    limit = DIFF_TEXT_LIMIT if limit is None else limit
    _, out, _ = await git(
        cwd,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
        base,
        head,
        timeout=TIMEOUT,
        env=env,
    )
    return out[:limit], len(out) > limit


async def detach(cwd: Path) -> None:
    """Leave the branch the worktree is on, so the owner can check it out elsewhere; the files stay."""
    await git(cwd, "checkout", "--quiet", "--detach")


async def remove_worktree(cwd: Path, path: Path) -> None:
    await git(cwd, "worktree", "remove", "--force", str(path))


async def prune_worktrees(cwd: Path) -> None:
    await git(cwd, "worktree", "prune", check=False)


async def delete_branch(cwd: Path, branch: str) -> None:
    await git(cwd, "branch", "--quiet", "-D", branch, check=False)
