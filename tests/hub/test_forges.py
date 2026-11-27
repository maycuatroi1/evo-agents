"""The forges as one interface (``evo_agents.hub.server.forge.models.forges``): which forge hosts an origin, the forge
of a change's name, the state a built change takes, and which repos the Loop reads."""

from __future__ import annotations

import pytest

from evo_agents.hub.server.forge.models.forges import (
    FORGES,
    GITHUB,
    GITLAB,
    Forge,
    GitHubForge,
    GitLabForge,
    built_state_of,
    forge_named,
    forge_of,
    github_target,
)
from evo_agents.hub.server.forge.models.github import Target
from evo_agents.hub.server.forge.models.gitlab import GitLabProject


@pytest.mark.parametrize(
    "origin",
    ["https://github.com/acme/app", "https://github.com/acme/app.git", "git@github.com:acme/app.git"],
)
def test_an_origin_on_github_com_is_github_with_its_owner_and_name(origin):
    assert forge_of(origin) is GITHUB
    assert GITHUB.target(origin) == Target("acme", "app")
    assert github_target(origin) == Target("acme", "app")
    assert GITHUB.hosts(origin)


@pytest.mark.parametrize(
    ("origin", "path"),
    [
        ("https://gitlab.example.com/group/app.git", "group/app"),
        ("git@gitlab.example.com:group/sub/app.git", "group/sub/app"),
    ],
)
def test_an_origin_on_another_host_is_gitlab_with_its_project(origin, path):
    assert forge_of(origin) is GITLAB
    assert GITLAB.project(origin) == GitLabProject("https://gitlab.example.com/api/v4", path)
    assert GITHUB.target(origin) is None
    assert not GITHUB.hosts(origin)


def test_a_github_com_origin_also_names_a_gitlab_project_so_github_is_asked_first():
    origin = "https://github.com/acme/app"
    assert GITLAB.hosts(origin)
    assert forge_of(origin) is GITHUB


@pytest.mark.parametrize("origin", [None, "", "not an origin", "https://gitlab.example.com/app", "/tmp/repo"])
def test_no_forge_hosts_no_origin_or_garbage(origin):
    assert forge_of(origin) is None
    assert github_target(origin) is None
    assert GITLAB.project(origin) is None


def test_the_registry_holds_each_forge_by_the_name_curator_changes_keeps():
    assert FORGES == {"github": GITHUB, "gitlab": GITLAB}
    assert forge_named("github") is GITHUB
    assert forge_named("gitlab") is GITLAB
    assert forge_named(None) is None
    assert forge_named("") is None
    assert forge_named("bitbucket") is None
    assert isinstance(GITHUB, GitHubForge) and isinstance(GITLAB, GitLabForge)
    assert all(isinstance(forge, Forge) for forge in FORGES.values())


def test_a_built_change_waits_for_its_pull_request_on_github_and_its_judge_elsewhere():
    assert GITHUB.built_state == "pr_pending"
    assert GITLAB.built_state == "judge_pending"
    assert built_state_of("github") == "pr_pending"
    assert built_state_of("gitlab") == "judge_pending"
    assert built_state_of(None) == "judge_pending"


def test_what_each_forge_does_with_a_change():
    assert (GITHUB.request, GITLAB.request) == ("pull request", "merge request")
    assert GITHUB.advanced_by_app and not GITLAB.advanced_by_app
    assert GITHUB.waits_on_rulesets and not GITLAB.waits_on_rulesets
    assert GITLAB.pushes_with_git_secret and not GITHUB.pushes_with_git_secret


def test_the_loop_reads_every_repo_on_github_and_a_gitlab_repo_only_in_a_mode_a_person_merges():
    github, gitlab = "https://github.com/acme/app", "https://gitlab.example.com/group/app"
    assert GITHUB.loop_reads(github, "app", frozenset())
    assert GITHUB.loop_reads(github, "app", frozenset({"app"}))
    assert not GITLAB.loop_reads(gitlab, "app", frozenset())
    assert GITLAB.loop_reads(gitlab, "app", frozenset({"app"}))
    assert not GITLAB.loop_reads(gitlab, "app", frozenset({"other"}))
