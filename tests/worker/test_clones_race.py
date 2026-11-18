"""Two runs of one repo reach a worker that has no checkout of it yet (``evo_agents.worker.runner.clones``): both see
it missing, one clones it under the lock of where it goes, and the other, which waited on that lock, takes the clone
with the run's leases handed to its origin, so its fetch goes through them rather than the machine's own credentials,
which a shared worker does not keep. A run that finds a checkout another run set up after it read the checkouts for its
leases hands them to that checkout's origin the same way."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

from evo_agents.worker import gitops
from evo_agents.worker.home import WorkerConfig
from evo_agents.worker.runner import clones

PROJECT = "demo"
REPO = "app"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def bare_origin(root: Path) -> Path:
    """A bare repo with one commit, the origin the runs clone from."""
    seed = root / "seed"
    seed.mkdir()
    git("init", "--quiet", "--initial-branch=main", cwd=seed)
    (seed / "README.md").write_text("app\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "seed", cwd=seed)
    origin = root / "origin.git"
    git("clone", "--quiet", "--bare", str(seed), str(origin), cwd=root)
    return origin


class Daemon:
    """What ``clones`` uses of the worker daemon (``RunHost``): its checkouts, a lock per place, its config and home."""

    def __init__(self, home: Path) -> None:
        self.checkouts: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.config = WorkerConfig(url="http://hub.invalid", worker_id=1, name="node", projects=[PROJECT])
        self.home = SimpleNamespace(root=home)
        self.env: dict[str, str] = {}

    def checkout_for(self, project: str, repo: str) -> Path | None:
        entry = self.checkouts.get(f"{project}/{repo}")
        return Path(entry["path"]) if entry else None

    def repo_lock(self, checkout: Path) -> asyncio.Lock:
        return self._locks.setdefault(str(checkout), asyncio.Lock())

    def add_checkout(self, project: str, repo: str, path: Path) -> None:
        self.checkouts[f"{project}/{repo}"] = {"path": str(path)}


class Credentials:
    """The run's leases as ``clones`` sees them: the origins they are handed to (``reach``), from the hub's answer."""

    def __init__(self, origin: Path) -> None:
        self.sources = {REPO: {"origin": str(origin)}}
        self.origins: dict[str, list[str]] = {}

    def reach(self, repo: str, urls) -> None:
        self.origins[repo] = list(urls)

    @contextlib.contextmanager
    def ticketed(self, base):
        yield dict(base)


class Run:
    def __init__(self, daemon: Daemon, origin: Path) -> None:
        self.project = PROJECT
        self.daemon = daemon
        self.credentials = Credentials(origin)
        self.notes: list[str] = []

    def note(self, text: str, **_extra) -> None:
        self.notes.append(text)

    def check(self) -> None:
        pass


def test_a_run_that_waited_while_another_cloned_takes_the_clone_with_its_own_leases(tmp_path, monkeypatch):
    origin = bare_origin(tmp_path)
    daemon = Daemon(tmp_path / "home")
    first, second = Run(daemon, origin), Run(daemon, origin)
    cloning = asyncio.Event()
    real_clone = gitops.clone

    async def slow_clone(url, path, *, env=None):
        cloning.set()
        await asyncio.sleep(0.2)  # the second run reaches the lock while the clone runs
        await real_clone(url, path, env=env)

    monkeypatch.setattr(clones.gitops, "clone", slow_clone)

    async def go():
        async def later():
            await cloning.wait()
            assert daemon.checkout_for(PROJECT, REPO) is None, "the second run starts while the clone is not done"
            await clones.ensure(second, [REPO])

        await asyncio.gather(clones.ensure(first, [REPO]), later())

    asyncio.run(go())
    target = tmp_path / "home" / "checkouts" / PROJECT / REPO
    assert daemon.checkout_for(PROJECT, REPO) == target
    assert git("rev-parse", "--is-inside-work-tree", cwd=target) == "true"
    starts = [text for text in first.notes + second.notes if text.startswith(f"Cloning {REPO} ")]
    assert len(starts) == 1, "one run clones, the other waits"
    assert any(text.startswith(f"No checkout of {REPO}") for text in second.notes), "both saw it missing"
    assert first.credentials.origins[REPO] == [str(origin)]
    assert second.credentials.origins.get(REPO) == [str(origin)], "the waiting run's leases reach the clone's origin"
    assert any(text.startswith(f"Another run set up the checkout of {REPO}") for text in second.notes)


def test_a_checkout_set_up_after_the_run_read_the_checkouts_gets_the_runs_leases(tmp_path):
    origin = bare_origin(tmp_path)
    daemon = Daemon(tmp_path / "home")
    target = tmp_path / "home" / "checkouts" / PROJECT / REPO
    git("clone", "--quiet", str(origin), str(target), cwd=tmp_path)
    late = Run(daemon, origin)  # read the checkouts for its leases while there was none: no origin handed them
    daemon.add_checkout(PROJECT, REPO, target)  # then another run set it up
    asyncio.run(clones.ensure(late, [REPO]))
    assert late.credentials.origins.get(REPO) == [str(origin)]
    assert not any(text.startswith("No checkout") or text.startswith("Cloning") for text in late.notes)


def test_a_checkout_whose_origin_the_run_holds_is_left_as_it_is(tmp_path):
    origin = bare_origin(tmp_path)
    daemon = Daemon(tmp_path / "home")
    target = tmp_path / "home" / "checkouts" / PROJECT / REPO
    git("clone", "--quiet", str(origin), str(target), cwd=tmp_path)
    daemon.add_checkout(PROJECT, REPO, target)
    run = Run(daemon, origin)
    run.credentials.origins[REPO] = ["https://example.invalid/app.git"]  # as the run's take handed them
    asyncio.run(clones.ensure(run, [REPO]))
    assert run.credentials.origins[REPO] == ["https://example.invalid/app.git"]
    assert run.notes == []
