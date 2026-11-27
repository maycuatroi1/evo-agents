"""The forges a repo of a project lives on, as one interface (Strategy): GitHub and GitLab, each a ``Forge`` whose
attributes say what the hub does with a change, a repo or a merge there, so that a caller asks the forge instead of
comparing its name.

``name`` is the value the column curator_changes.forge holds (``forge_named`` finds the forge of it); ``forge_of``
finds the forge that hosts an origin: GitHub for one on github.com, else GitLab when ``project_of`` names a project
of it (it names one for a github.com origin too, so GitHub is asked first). ``FORGES`` is the registry, by name.
Nothing here does I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server.forge.models.github import Target
from evo_agents.hub.server.forge.models.gitlab import GitLabProject, project_of


class Forge(ABC):
    """A forge the hub drives the Curator's changes and reads the merges of plan runs on."""

    name: ClassVar[str]  # "github" or "gitlab", as curator_changes.forge holds it
    built_state: ClassVar[str]  # the state a Curator change takes once its Builder's plan is done
    request: ClassVar[str]  # what a change asks to merge there: "pull request" or "merge request"
    advanced_by_app: ClassVar[bool]  # the hub's job opens, reads and merges its changes' requests with its GitHub App
    waits_on_rulesets: ClassVar[bool]  # a Builder runs there once a ruleset of each repo keeps the Curator's App off
    pushes_with_git_secret: ClassVar[bool]  # a Builder pushes there with the owner's git secret the charter names

    @abstractmethod
    def hosts(self, origin: str | None) -> bool:
        """Whether ``origin`` is a repo of this forge."""

    @abstractmethod
    def loop_reads(self, origin: str, repo: str, by_hand: frozenset[str]) -> bool:
        """Whether the Loop reads what ``repo`` at ``origin``, a repo of this forge, merged; ``by_hand`` names the
        project's repos in a mode a person merges."""

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


class GitHubForge(Forge):
    """github.com: a change opens a pull request the hub's job reads, judges and merges through its GitHub App."""

    name = "github"
    built_state = "pr_pending"
    request = "pull request"
    advanced_by_app = True
    waits_on_rulesets = True
    pushes_with_git_secret = False

    def target(self, origin: str | None) -> Target | None:
        """owner/name of ``origin`` on GitHub; None for an origin elsewhere, or none."""
        found = github_repo(origin) if origin else None
        return Target(*found) if found else None

    def hosts(self, origin: str | None) -> bool:
        return self.target(origin) is not None

    def loop_reads(self, origin: str, repo: str, by_hand: frozenset[str]) -> bool:
        """Every repo on GitHub."""
        return self.hosts(origin)


class GitLabForge(Forge):
    """A GitLab host: the Builder's push opens the merge request, and the merge queue merges it with a forge
    secret."""

    name = "gitlab"
    built_state = "judge_pending"
    request = "merge request"
    advanced_by_app = False
    waits_on_rulesets = False
    pushes_with_git_secret = True

    def project(self, origin: str | None) -> GitLabProject | None:
        """The GitLab project of ``origin``; None without one."""
        return project_of(origin) if origin else None

    def hosts(self, origin: str | None) -> bool:
        return self.project(origin) is not None

    def loop_reads(self, origin: str, repo: str, by_hand: frozenset[str]) -> bool:
        """A repo on GitLab in a mode a person merges alone: the merge queue reads the others."""
        return self.hosts(origin) and repo in by_hand


GITHUB = GitHubForge()
GITLAB = GitLabForge()
FORGES: dict[str, Forge] = {forge.name: forge for forge in (GITHUB, GITLAB)}


def forge_named(name: str | None) -> Forge | None:
    """The forge of ``name``, as curator_changes.forge holds it; None for none or an unknown one."""
    return FORGES.get(name) if name else None


def forge_of(origin: str | None) -> Forge | None:
    """The forge that hosts ``origin``: GitHub for a repo on github.com, else GitLab when it names a project of a
    host; None without a usable origin."""
    if GITHUB.hosts(origin):
        return GITHUB
    return GITLAB if GITLAB.hosts(origin) else None


def github_target(origin: str | None) -> Target | None:
    """owner/name of ``origin`` on GitHub; None for an origin elsewhere, or none (``GITHUB.target``)."""
    return GITHUB.target(origin)


def built_state_of(name: str | None) -> str:
    """The state a Curator change on forge ``name`` takes once its Builder's plan is done; a change of no known forge
    waits for its Judge, as GitLab's does."""
    return (forge_named(name) or GITLAB).built_state
