"""What a kind of run brings to the Run that holds it (``RunKind``), the base of every kind of ``kinds.KINDS``.

A kind gets the Run in its constructor and works through the parts the Run holds. It declares what it may do
(``traits``, a ``run_kinds.Kind``: what it pushes, whether it runs in a container, which commands its agent uses) and
how its worktrees end, and implements ``steps``. A kind with a directory of worktrees (``directory``) gets the rest
from this base: the watchdog compares them (``watched``), their diff is uploaded once the run ended (``diff``), and
they leave the plan's branches (``release_branch``), each unless the kind says otherwise."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from evo_agents.worker.run_kinds import Kind
    from evo_agents.worker.runner.context import Run
    from evo_agents.worker.runner.directory import RunDirectory


class RunKind:
    """A kind of run (see the module)."""

    traits: ClassVar[Kind]
    parks: ClassVar[bool] = False  # it waits for its owner between turns, and the hub may park it: its traits say
    chat: ClassVar[bool] = False  # its owner ends its chat
    watches: ClassVar[bool] = True  # the watchdog compares the protected paths of its worktrees
    uploads_diff: ClassVar[bool] = True  # the diff of its commits is uploaded once it ended
    leaves_branch: ClassVar[bool] = True  # its worktrees leave the plan's branches once it is over

    run: Run
    directory: RunDirectory | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "traits" in cls.__dict__:
            cls.parks = cls.traits.parks

    def __init__(self, run: Run):
        self.run = run

    async def steps(self) -> None:
        """From the claim to the report that ends the run; RunFailed or Stopped ends it otherwise."""
        raise NotImplementedError

    def watched(self) -> list[tuple[str, Path, str]]:
        """(repo, worktree, the commit it started at) of each worktree of the run, for the watchdog."""
        if self.directory is None or not self.watches:
            return []
        return self.directory.watched()

    async def diff(self) -> bytes:
        """The diff of the run's commits, uploaded once it ended; empty for none."""
        if self.directory is None or not self.uploads_diff:
            return b""
        return await self.directory.diff()

    async def release_branch(self) -> None:
        """Leave the plan's branches once the run is over, so they can be checked out elsewhere."""
        if self.directory is not None and self.leaves_branch:
            await self.directory.release()
