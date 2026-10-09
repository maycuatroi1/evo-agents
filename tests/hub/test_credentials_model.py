"""The credential model of evo_agents.hub.credentials, without Postgres or a server.

The checks step 2 of the worker-credentials plan names: normalize_origin on the four real origins of the hub's
projects, DENIED_ENV refuses PATH and EVO_HUB_URL, and repr(Lease) does not carry the value."""

from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.hub import credentials
from evo_agents.hub.credentials import (
    DENIED_ENV,
    Lease,
    env_name_refusal,
    github_repo,
    is_ssh_origin,
    matches,
    normalize_origin,
)


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://github.com/maycuatroi1/evo-agents.git", "https://github.com/maycuatroi1/evo-agents"),
        ("https://github.com/hawkteam404/m1-identity.git", "https://github.com/hawkteam404/m1-identity"),
        ("git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git", "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs"),
        ("ssh://git@gitlab.m1ops.com/fis-gb-m1/m1-kb-docs", "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs"),
        ("ssh://git@gitlab.m1ops.com:2222/fis-gb-m1/m1-kb-docs.git", "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs"),
        ("https://user@GitHub.com/maycuatroi1/evo-agents/", "https://github.com/maycuatroi1/evo-agents"),
        ("https://git.example.com:8443/a/b.git", "https://git.example.com:8443/a/b"),
    ],
)
def test_normalize_origin(origin, expected):
    assert normalize_origin(origin) == expected


def test_ssh_origins():
    assert is_ssh_origin("git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git")
    assert is_ssh_origin("ssh://git@gitlab.m1ops.com/fis-gb-m1/m1-kb-docs")
    assert not is_ssh_origin("https://github.com/maycuatroi1/evo-agents.git")
    assert not is_ssh_origin("/tmp/repo.git")


def test_matches_whole_path_parts():
    prefix = "https://gitlab.m1ops.com/fis-gb-m1/m1-kb-docs"
    assert matches(prefix, "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git")
    assert matches(prefix, "ssh://git@gitlab.m1ops.com/fis-gb-m1/m1-kb-docs")
    assert matches("https://gitlab.m1ops.com/fis-gb-m1", "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git")
    assert not matches(prefix, "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs-old.git")
    assert not matches("https://gitlab.m1ops.com/fis", "git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git")
    assert not matches(prefix, "https://github.com/maycuatroi1/evo-agents.git")
    assert not matches("ssh-not-a-url", "ssh-not-a-url")


def test_github_repo():
    assert github_repo("https://github.com/hawkteam404/m1-identity.git") == ("hawkteam404", "m1-identity")
    assert github_repo("git@github.com:maycuatroi1/evo-agents.git") == ("maycuatroi1", "evo-agents")
    assert github_repo("git@gitlab.m1ops.com:fis-gb-m1/m1-kb-docs.git") is None
    assert github_repo("https://github.com/maycuatroi1") is None


def test_denied_env():
    assert "PATH" in DENIED_ENV
    assert env_name_refusal("PATH")
    assert env_name_refusal("EVO_HUB_URL")
    for name in ("GIT_ASKPASS", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "HOME", "SSH_AUTH_SOCK"):
        assert env_name_refusal(name), name
    for name in ("lower", "1ABC", "A-B", ""):
        assert env_name_refusal(name), name
    assert env_name_refusal("CLAUDE_CODE_OAUTH_TOKEN") is None
    assert env_name_refusal("OPENAI_API_KEY") is None


def test_lease_repr_leaves_out_the_value():
    value = "sk-ant-oat01-do-not-print-me"
    expires = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    lease = Lease(1, "env", "secret", "claude", env_var="CLAUDE_CODE_OAUTH_TOKEN", value=value, expires_at=expires)
    for text in (repr(lease), str(lease), f"{lease}", repr([lease]), repr({"lease": lease})):
        assert value not in text
        assert "CLAUDE_CODE_OAUTH_TOKEN" in text
    assert Lease.from_json(lease.to_json()) == lease


def test_a_lease_from_the_hub_is_read_with_a_trailing_z():
    """The hub's JSON writes a UTC time as ``...Z``, with or without a fraction of a second."""
    sent = {"id": 2, "kind": "git", "provider": "github-app", "expires_at": "2026-10-07T00:41:08Z"}
    assert Lease.from_json(sent).expires_at == datetime(2026, 10, 7, 0, 41, 8, tzinfo=UTC)
    sent["expires_at"] = "2026-10-07T00:41:08.123Z"
    assert Lease.from_json(sent).expires_at == datetime(2026, 10, 7, 0, 41, 8, 123000, tzinfo=UTC)


def test_github_lease_needs_refresh_near_its_end():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    left = timedelta(seconds=credentials.GITHUB_TOKEN_REFRESH_SECONDS)
    github = Lease(2, "git", "github-app", "github-app:maycuatroi1", url_prefix="https://github.com/a/b", value="t")
    assert not github.needs_refresh(now)
    assert not Lease(**{**github.__dict__, "expires_at": now + left + timedelta(seconds=1)}).needs_refresh(now)
    assert Lease(**{**github.__dict__, "expires_at": now + left - timedelta(seconds=1)}).needs_refresh(now)
    secret = Lease(3, "git", "secret", "gitlab", url_prefix="https://h/a", value="t", expires_at=now)
    assert not secret.needs_refresh(now)


def test_constants():
    assert credentials.SECRET_KINDS == ("env", "git")
    assert credentials.PROVIDERS == ("secret", "github-app")
    assert credentials.GITHUB_PERMISSIONS == {"contents": "write", "metadata": "read"}
    assert credentials.MAX_SECRET_BYTES == 16384
    assert credentials.MAX_SECRETS_PER_OWNER == 200


def test_who_may_dispatch_to_a_worker_and_the_credential_a_dispatch_is_recorded_with():
    assert credentials.DISPATCH_FROM == ("any", "web")
    assert credentials.DISPATCHED_VIA == ("machine", "web", "schedule")  # schedule: the night shift (schema 0012)
    # a web session, a machine token and the worker token of a run's agent on /mcp
    kinds = ("web", "machine", "worker")
    assert [credentials.dispatch_credential(kind) for kind in kinds] == ["web", "machine", "machine"]
