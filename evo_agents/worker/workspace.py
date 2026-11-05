"""The repos of a plan run on this worker (``Workspace``), as the daemon keeps them in the run's record, and the names
of their worktrees (``folder_name``) and of the branches its plan names for them (``plan_branches``). ``gitops``
re-exports them."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path


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
