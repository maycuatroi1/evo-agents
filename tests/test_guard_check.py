"""The guards of private names on a machine (``evo_agents.private_names``, ``evo_agents.guard_check``,
``docs/guard.md``): the matcher finds a hashed term in any case and inside joined words, a commit message, a push and
the text of a ``gh`` command bound for a guarded repo are refused, and the same text bound elsewhere passes. The names
are fictional (northwind, lumen).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evo_agents import guard_check, private_names
from evo_agents.private_names import Policy, entries, repo_key

TARGET = "github.com/acme/public"


def policy(*terms: str, targets=(TARGET,)) -> Policy:
    return Policy(terms=[e for term in terms for e in entries(term)], targets=list(targets))


@pytest.fixture
def policy_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy("Northwind", "lumen ops").to_json()))
    monkeypatch.setenv(guard_check.ENV, str(path))
    return path


def git(cwd: Path, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@x"
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check, env=env, **kw)


@pytest.fixture
def repo(tmp_path) -> Path:
    """A checkout whose origin is the guarded repo, with one commit on its remote-tracking main."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", "git@github.com:Acme/Public.git")
    (work / "a.txt").write_text("hello\n- a line that stays\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "start")
    git(work, "update-ref", "refs/remotes/origin/main", "HEAD")
    return work


def test_the_matcher_finds_a_hashed_term_by_its_words_in_any_case_and_inside_joined_words():
    found = policy("Northwind", "lumen ops", "fir-gb").matcher()
    text = "NorthWind, northwindCLI, northwind_cli, Lumen-Ops, lumenops, fir gb, firgb, north wind, lumen"
    assert [h.found for h in found.find(text)] == [
        "NorthWind",
        "northwind",
        "northwind",
        "Lumen-Ops",
        "lumenops",
        "fir gb",
        "firgb",
    ]
    assert not found.find("northern winds, illumination, operations")
    assert not found.find("lumen\nops, (lumen): ops, lumen ...  ops")  # the words of a term never across a line
    data = policy("Northwind").to_json()
    assert "northwind" not in json.dumps(data).lower()
    assert Policy.from_json(data).digest() == data["digest"]
    with pytest.raises(ValueError):
        Policy.from_json({**data, "version": 2})


def test_a_repo_reads_the_same_from_every_way_git_and_gh_write_it():
    for url in (
        "git@github.com:Acme/Public.git",
        "https://x-access-token:tok@github.com/acme/public",
        "ssh://git@github.com/acme/public.git",
        "acme/public",
    ):
        assert repo_key(url) == TARGET, url
    assert repo_key("ssh://git@host:2222/group/sub/repo.git") == "host/group/sub/repo"
    wild = policy("Northwind", targets=["github.com/acme/open-*"])
    assert wild.guards("acme/open-kit") and not wild.guards("acme/public")


def test_outgoing_reads_the_messages_paths_and_added_lines_of_what_a_push_sends(repo):
    (repo / "a.txt").write_text("hello\nan added line\n")
    (repo / "notes-dir").mkdir()
    (repo / "notes-dir" / "b.txt").write_text("b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "change\n\n- a bullet of the message")
    text = guard_check.outgoing(repo, "HEAD", remote="origin")
    assert "- a bullet of the message" in text and "an added line" in text and "notes-dir/b.txt" in text
    assert "a line that stays" not in text and "start" not in text


def test_the_commit_msg_and_pre_push_hooks_refuse_a_name_bound_for_a_guarded_repo(repo, policy_file, monkeypatch):
    monkeypatch.chdir(repo)
    message = repo / "MSG"
    message.write_text("Fix the brief of northwind\n# a comment northwind is not checked\n")
    assert guard_check.hook("commit-msg", [str(message)], "") == guard_check.REFUSED
    message.write_text("Fix the brief\n# northwind in a comment\n")
    assert guard_check.hook("commit-msg", [str(message)], "") == 0
    (repo / "c.txt").write_text("lumen-ops pulls the image\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "images")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    base = git(repo, "rev-parse", "origin/main").stdout.strip()
    with pytest.raises(guard_check.Refused, match="lumen-ops"):
        guard_check.check_push(
            guard_check.load(), repo, "git@github.com:acme/public.git", [("main", head, "main", base)]
        )
    guard_check.check_push(guard_check.load(), repo, "git@github.com:acme/private.git", [("main", head, "main", base)])


def test_a_hook_in_a_checkout_without_a_guarded_remote_or_without_a_policy_lets_everything_through(
    repo, tmp_path, policy_file, monkeypatch
):
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/internal.git")
    message = tmp_path / "MSG"
    message.write_text("northwind\n")
    monkeypatch.chdir(repo)
    assert guard_check.hook("commit-msg", [str(message)], "") == 0
    monkeypatch.setenv(guard_check.ENV, str(tmp_path / "missing.json"))
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/public.git")
    assert guard_check.hook("commit-msg", [str(message)], "") == 0


@pytest.mark.parametrize(
    ("args", "checked"),
    [
        (["pr", "create", "--title", "t", "--body", "for northwind"], True),
        (["pr", "edit", "12", "-b", "x", "-R", "acme/public"], True),
        (["issue", "comment", "3", "--body=b"], True),
        (["release", "create", "v1", "--notes", "n"], True),
        (["api", "-X", "PATCH", "repos/acme/public/pulls/1", "-f", "body=hi"], True),
        (["pr", "view", "12"], False),
        (["api", "user"], False),
        (["repo", "view"], False),
    ],
)
def test_gh_texts_names_the_commands_that_write_text_and_what_they_write(args, checked):
    found = guard_check.gh_texts(args, [None])
    assert (found is not None) is checked


@pytest.mark.skipif(sys.platform == "win32", reason="the gh shim and the fake gh are POSIX shell scripts")
def test_the_gh_shim_refuses_a_name_for_a_guarded_repo_and_runs_the_real_gh_otherwise(tmp_path, repo, policy_file):
    real = tmp_path / "real-bin"
    real.mkdir()
    (real / "gh").write_text('#!/bin/sh\necho "real gh $*"\n')
    (real / "gh").chmod(0o755)
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "gh").write_text(f'#!/bin/sh\nexec {sys.executable} -m evo_agents.guard_check gh "$@"\n')
    (shim / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{real}{os.pathsep}{os.environ['PATH']}"}
    env[guard_check.SHIM_ENV] = str(shim)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    body = tmp_path / "body.md"
    body.write_text("Opened for Northwind\n")

    def gh(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["gh", *args], cwd=repo, env=env, capture_output=True, text=True, check=False)

    refused = gh("pr", "create", "--title", "t", "--body-file", str(body))
    assert refused.returncode == guard_check.REFUSED and "Northwind" in refused.stderr and not refused.stdout
    passed = gh("pr", "create", "--title", "t", "--body", "clean")
    assert passed.returncode == 0 and passed.stdout.strip() == "real gh pr create --title t --body clean"
    elsewhere = gh("pr", "create", "--title", "northwind", "-R", "acme/internal")
    assert elsewhere.returncode == 0
    piped = subprocess.run(
        ["gh", "pr", "comment", "1", "--body-file", "-"],
        cwd=repo,
        env=env,
        input="lumen ops",
        capture_output=True,
        text=True,
        check=False,
    )
    assert piped.returncode == guard_check.REFUSED


def test_refusal_shows_each_name_as_written_and_never_the_policy():
    hits = policy("Northwind").matcher().find("a brief of NORTHWIND today")
    text = private_names.refusal(hits, "the PR body")
    assert "'NORTHWIND'" in text and "fictional name" in text


def test_a_push_to_the_guarded_remote_reads_a_commit_another_remote_has_other_remote(repo, policy_file, monkeypatch):
    """A commit a fork already has was never checked against the guarded origin: the push to origin still reads it,
    from the push remote of a bare ``git push`` and from the remote a pre-push hook names."""
    git(repo, "remote", "add", "fork", "git@github.com:someone/fork.git")
    (repo / "c.txt").write_text("lumen ops in the fork\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "fork work")
    git(repo, "update-ref", "refs/remotes/fork/main", "HEAD")
    assert "lumen ops" in guard_check.outgoing(repo, "HEAD")
    assert "lumen ops" in guard_check.outgoing(repo, "HEAD", remote="origin")
    assert "lumen ops" not in guard_check.outgoing(repo, "HEAD", remote="fork")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(repo)
    stdin = f"refs/heads/main {head} refs/heads/main {guard_check.ZERO}\n"
    assert guard_check.hook("pre-push", ["origin", "git@github.com:acme/public.git"], stdin) == guard_check.REFUSED
    unknown = "1" * 40  # the remote moved to a commit this checkout never fetched: what origin has is read instead
    stdin = f"refs/heads/main {head} refs/heads/main {unknown}\n"
    assert guard_check.hook("pre-push", ["origin", "git@github.com:acme/public.git"], stdin) == guard_check.REFUSED


def test_the_guard_refuses_a_push_when_git_fails_or_runs_out_of_time_git_fails(repo, policy_file, monkeypatch):
    from evo_agents.worker.gitops import PushRefused
    from evo_agents.worker.guard import check_push

    with pytest.raises(guard_check.Unread, match="failed"):
        guard_check.outgoing(repo, "no-such-ref", remote="origin")
    monkeypatch.setattr(guard_check, "_git", lambda cwd, *args: (124, ""))
    with pytest.raises(guard_check.Unread, match="ran out of time"):
        guard_check.outgoing(repo, "HEAD", remote="origin")
    monkeypatch.chdir(repo)
    stdin = f"refs/heads/main {'2' * 40} refs/heads/main {guard_check.ZERO}\n"
    assert guard_check.hook("pre-push", ["origin", "git@github.com:acme/public.git"], stdin) == guard_check.REFUSED
    with pytest.raises(PushRefused, match="could not read"):
        check_push(repo, "HEAD", "main", "git@github.com:acme/public.git", None)


@pytest.mark.parametrize(
    ("args", "refused"),
    [
        (["api", "graphql", "-f", 'query=mutation { addComment(input: {body: "northwind"}) { clientMutationId } }'], 1),
        (["api", "graphql", "-F", "body=lumen ops", "-f", "query=mutation($body: String!) { x(body: $body) }"], 1),
        (["api", "graphql", "-f", 'query=query { search(query: "northwind") { issueCount } }'], 0),
        (["api", "graphql", "-f", 'query=mutation { x(body: "clean") }'], 0),
    ],
)
def test_the_gh_guard_reads_a_graphql_mutation_graphql(repo, policy_file, monkeypatch, args, refused):
    monkeypatch.chdir(repo)
    monkeypatch.delenv("GH_REPO", raising=False)
    if refused:
        with pytest.raises(guard_check.Refused, match="northwind|lumen"):
            guard_check.gh_check(guard_check.load(), args, repo, [None])
    else:
        guard_check.gh_check(guard_check.load(), args, repo, [None])


def test_a_graphql_mutation_names_its_repo_by_owner_and_name_graphql(repo, policy_file, monkeypatch):
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/internal.git")
    monkeypatch.delenv("GH_REPO", raising=False)
    mutation = 'query=mutation($owner: String!, $name: String!) { x(body: "northwind") }'
    args = ["api", "graphql", "-F", "owner=acme", "-F", "name=public", "-f", mutation]
    with pytest.raises(guard_check.Refused):
        guard_check.gh_check(guard_check.load(), args, repo, [None])
    guard_check.gh_check(guard_check.load(), ["api", "graphql", "-f", mutation], repo, [None])


def test_the_owner_repo_placeholder_reads_as_the_checkouts_repo_placeholder(repo, policy_file, monkeypatch):
    monkeypatch.delenv("GH_REPO", raising=False)
    args = ["api", "-X", "POST", "repos/{owner}/{repo}/issues/1/comments", "-f", "body=for northwind"]
    assert guard_check.gh_texts(args, [None]) == (None, ["body for northwind"])
    with pytest.raises(guard_check.Refused):
        guard_check.gh_check(guard_check.load(), args, repo, [None])
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/internal.git")
    guard_check.gh_check(guard_check.load(), args, repo, [None])
    monkeypatch.setenv("GH_REPO", "acme/public")
    with pytest.raises(guard_check.Refused):
        guard_check.gh_check(guard_check.load(), args, repo, [None])
