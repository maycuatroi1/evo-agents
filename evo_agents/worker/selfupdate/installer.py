"""Where the tag nightly points, and the update of an install to it.

The lookup asks ``git ls-remote`` of the repository (no API quota), then the GitHub API when git fails.

``uv tool install`` was checked with uv 0.6.13 (2026-10-19) before the update leaned on it. It is not atomic: over an
environment made from the same interpreter it replaces the changed packages in place, file by file; asked for another
interpreter it deletes the environment first, and when the build then fails nothing is left, not even the command. A
build or resolution that fails over the same interpreter leaves the old install as it was, and an install whose code
does not import is put in place without a word. Hence the update:

1. ``uv tool install`` of ``evo-ak[<extras>] @ git+<repo>@nightly`` into a staging directory, then the staged install
   started (``python -c 'import evo_agents.worker.daemon'`` and ``evo-agents --version``) and its commit read. A
   failure there leaves the install untouched.
2. The same over the install, with ``--python`` its own interpreter, so uv replaces the packages in place and never
   deletes the environment, and ``--reinstall-package``/``--refresh-package`` so a tag that moved is read again. The
   install is then started and its commit checked against the staged one (or nightly read again, when the tag moved
   between the two).
3. When the install does not start or carries another commit, the old one is put back: ``@<commit>`` for an install
   from git, ``==<version>`` for one from PyPI, and started again.

The daemons of the install claim nothing meanwhile (``peers``), so no run's code changes under it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import subprocess
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.worker.selfupdate.install import (
    COMMAND,
    COMMIT,
    PACKAGE,
    REPO_URL,
    TAG,
    Identity,
    Install,
    read_identity,
    version_of,
)

LOOKUP_TIMEOUT = 30.0
INSTALL_TIMEOUT = 1200.0  # one uv tool install, from git, with its dependencies
PROBE_TIMEOUT = 120.0
API_URL = "https://api.github.com/repos/maycuatroi1/evo-agents/git/ref/tags/" + TAG
PROBE = "import evo_agents.cli, evo_agents.worker.daemon"
TAIL = 600  # characters of a command's output an error keeps
DROPPED = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")  # of the daemon's environment, for uv and the probes


@dataclass(frozen=True)
class Done:
    code: int | None  # None: it did not start, or did not finish in time
    output: str


async def run_command(argv: list[str], *, env: Mapping[str, str] | None, timeout: float) -> Done:
    """Run ``argv`` with stdout and stderr together, in a session of its own (an install goes on when launchd stops
    the daemon), and kill it after ``timeout`` seconds."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=None if env is None else dict(env),
            start_new_session=True,
        )
    except OSError as exc:
        return Done(None, f"{argv[0]} did not start: {exc}")
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return Done(None, f"{Path(argv[0]).name} did not finish within {timeout:g}s")
    return Done(proc.returncode, output.decode("utf-8", errors="replace"))


def tail(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= TAIL else "..." + text[-TAIL:]


# Where nightly points


def parse_ls_remote(output: str) -> str | None:
    """The commit of the tag in ``git ls-remote`` lines: the peeled ``^{}`` line of an annotated tag first."""
    found = {}
    for line in output.splitlines():
        sha, _, ref = line.strip().partition("\t")
        found[ref] = sha
    commit = found.get(f"refs/tags/{TAG}^{{}}") or found.get(f"refs/tags/{TAG}")
    return commit if commit and COMMIT.match(commit) else None


def fetch_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=LOOKUP_TIMEOUT) as response:
        return json.load(response)


def api_commit(fetch: Callable[[str], dict] = fetch_json) -> str:
    target = fetch(API_URL)["object"]
    if target.get("type") == "tag":  # an annotated tag: its object names the commit
        target = fetch(target["url"])["object"]
    commit = target.get("sha")
    if not isinstance(commit, str) or not COMMIT.match(commit):
        raise ValueError(f"the GitHub API named no commit for the tag {TAG}")
    return commit


async def nightly_commit(env: Mapping[str, str], *, runner=run_command, fetch=fetch_json) -> str:
    """The commit the tag nightly points to; LookupError says why it could not be read."""
    errors = []
    git = shutil.which("git", path=env.get("PATH"))
    if git:
        ref = f"refs/tags/{TAG}"
        argv = [git, "ls-remote", REPO_URL, ref, ref + "^{}"]
        done = await runner(argv, env={**env, "GIT_TERMINAL_PROMPT": "0"}, timeout=LOOKUP_TIMEOUT)
        commit = parse_ls_remote(done.output) if done.code == 0 else None
        if commit:
            return commit
        errors.append(f"git ls-remote: {tail(done.output) or f'no tag {TAG}'}")
    else:
        errors.append("git is not on PATH")
    try:
        return await asyncio.to_thread(api_commit, fetch)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f"GitHub API: {exc}")
    raise LookupError(f"cannot read where {TAG} points: {'; '.join(errors)}")


# The update


@dataclass(frozen=True)
class Outcome:
    ok: bool
    commit: str | None = None  # the commit installed, when ok
    error: str | None = None  # what failed, when not
    broken: bool = False  # the install may not start: putting the old one back failed too


class Installer:
    def __init__(
        self,
        install: Install,
        uv: str,
        staging: Path,
        env: Mapping[str, str],
        *,
        runner=run_command,
        install_timeout: float = INSTALL_TIMEOUT,
        probe_timeout: float = PROBE_TIMEOUT,
    ):
        self.install = install
        self.uv = uv
        self.staging = Path(staging)
        self.env = {key: value for key, value in env.items() if key not in DROPPED}
        self.runner = runner
        self.install_timeout = install_timeout
        self.probe_timeout = probe_timeout

    def requirement(self, revision: str = TAG) -> str:
        return f"{PACKAGE}{self.install.requirement_extras} @ git+{REPO_URL}@{revision}"

    def previous(self) -> str | None:
        """The requirement that puts the install back as it was."""
        if self.install.kind == "git" and self.install.commit:
            return self.requirement(self.install.commit)
        if self.install.kind == "pypi" and self.install.version:
            return f"{PACKAGE}{self.install.requirement_extras}=={self.install.version}"
        return None

    async def _uv_install(self, tool_dir: Path, bin_dir: Path, requirement: str) -> Done:
        argv = [self.uv, "tool", "install", "--force", "--reinstall-package", PACKAGE, "--refresh-package", PACKAGE]
        argv += ["--python", str(self.install.python), "--no-progress", requirement]
        env = {**self.env, "UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(bin_dir), "NO_COLOR": "1"}
        return await self.runner(argv, env=env, timeout=self.install_timeout)

    async def check(self, prefix: Path, entrypoint: Path) -> tuple[str | None, Identity]:
        """Whether the install at ``prefix`` starts: what failed (None when it did), and what it holds."""
        python = prefix / "bin" / "python"
        for argv in ([str(python), "-c", PROBE], [str(entrypoint), "--version"]):
            done = await self.runner(argv, env=self.env, timeout=self.probe_timeout)
            if done.code != 0:
                status = "did not finish" if done.code is None else f"exited {done.code}"
                return f"{' '.join(argv)} {status}: {tail(done.output)}", read_identity(prefix)
        return None, read_identity(prefix)

    async def _stage(self) -> tuple[str | None, str | None]:
        """Install nightly into the staging directory and start it: what failed, and the commit staged."""
        shutil.rmtree(self.staging, ignore_errors=True)
        try:
            tools, bin_dir = self.staging / "tools", self.staging / "bin"
            done = await self._uv_install(tools, bin_dir, self.requirement())
            if done.code != 0:
                return f"uv could not install {self.requirement()}: {tail(done.output)}", None
            problem, staged = await self.check(tools / PACKAGE, bin_dir / COMMAND)
            if problem:
                return f"the new version does not start: {problem}", None
            if staged.commit is None:
                return "the new version names no commit", None
            return None, staged.commit
        finally:
            shutil.rmtree(self.staging, ignore_errors=True)

    async def update(self, latest: Callable[[], Awaitable[str]]) -> Outcome:
        """Update the install to nightly; ``latest`` reads where nightly points again, for a tag that moved."""
        problem, staged = await self._stage()
        if problem:
            return Outcome(False, error=problem)
        install = self.install
        done = await self._uv_install(install.prefix.parent, install.entrypoint.parent, self.requirement())
        problem, found = await self.check(install.prefix, install.entrypoint)
        if done.code == 0 and problem is None:
            wanted = {staged}
            if found.commit != staged:
                with contextlib.suppress(LookupError):
                    wanted.add(await latest())
            if found.commit in wanted:
                return Outcome(True, commit=found.commit)
            problem = f"the install holds {found.commit}, not nightly {staged}"
        elif done.code != 0:
            problem = f"uv could not install {self.requirement()} over the install: {tail(done.output)}"
        return await self._put_back(problem)

    def _as_before(self, found: Identity) -> bool:
        return version_of(found) == self.install.version and found.commit == self.install.commit

    async def _put_back(self, why: str) -> Outcome:
        """The update failed: when the install no longer is what it was, install that again."""
        install = self.install
        problem, found = await self.check(install.prefix, install.entrypoint)
        if problem is None and self._as_before(found):
            return Outcome(False, error=f"{why}; the install is as it was")
        previous = self.previous()
        if previous is None:
            return Outcome(False, error=f"{why}; nothing to put back", broken=True)
        done = await self._uv_install(install.prefix.parent, install.entrypoint.parent, previous)
        problem, found = await self.check(install.prefix, install.entrypoint)
        if done.code == 0 and problem is None and self._as_before(found):
            return Outcome(False, error=f"{why}; put back {previous}")
        trouble = problem or f"uv exited {done.code}: {tail(done.output)}"
        return Outcome(False, error=f"{why}; putting back {previous} failed too: {trouble}", broken=True)
