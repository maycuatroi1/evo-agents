"""``evo-agents hub account list|set|pause|resume|policy``, as a signed-in member runs them: the commands and their
``--json`` keys are in the contract (seam hub-cli-v1), a number out of bounds is refused before the hub is asked, and
against a hub no command prints a value.

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

import io

import pytest

from evo_agents.hub.contract import commands
from tests.hub import pg
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok
from tests.hub.test_secrets_cli import needs_pg, nowhere, run_cli, sample

VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def account_cli(monkeypatch, capsys, home, *args: str):
    return run_cli(monkeypatch, capsys, home, "hub", "account", *args)


# Without a hub


def test_the_account_commands_are_in_the_contract():
    printed = commands()
    names = {"hub account list", "hub account set", "hub account pause", "hub account resume", "hub account policy"}
    assert names <= set(printed)
    assert printed["hub account list"]["json"]["schema"] == "Account"
    assert printed["hub account list"]["json"]["kind"] == "array"
    for command in ("hub account set", "hub account pause", "hub account resume"):
        assert printed[command]["json"]["schema"] == "Account"
        assert "value" not in printed[command]["json"]["keys"]
    assert printed["hub account policy"]["json"]["schema"] == "AccountPolicy"
    options = {o["flags"][0]: o for o in printed["hub account policy"]["options"]}
    assert options["--strategy"]["choices"] == ["round-robin", "weighted-round-robin", "fill-first"]


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        (("set", "claude-a", "--weight", "1001"), "--weight is a whole number from 0 to 1000, not 1001"),
        (("set", "claude-a", "--priority", "-1"), "--priority is a whole number from 0 to 1000, not -1"),
        (("set", "claude-a"), "give --weight, --priority or both"),
        (("policy", "evo-agents", "--cooldown", "59"), "--cooldown is a whole number from 60 to 604800, not 59"),
        (("policy", "evo-agents", "--max-failovers", "21"), "--max-failovers is a whole number from 0 to 20, not 21"),
        (("policy", "evo-agents", "--reset", "--strategy", "fill-first"), "--reset drops the policy: give it alone"),
    ],
)
def test_a_number_out_of_bounds_is_refused_before_the_hub_is_asked(monkeypatch, capsys, tmp_path, args, problem):
    result = account_cli(monkeypatch, capsys, tmp_path, *args)
    assert result.code == 2 and result.out == ""
    assert result.err == f"error: {problem}\n"


# Against a hub

if pg.DSN:
    from tests.hub import test_credentials_api as leases_api
    from tests.hub.test_runs import PROJECT

    # the hub of test_secrets_cli, as fixtures of this module
    app_key, hub = secrets_cli.app_key, secrets_cli.hub


@needs_pg
def test_list_set_pause_and_policy_without_ever_printing_a_value(hub, monkeypatch, capsys):
    owner, other = hub.homes["owner"], hub.homes["other"]
    values = {name: sample(f"{name}-") for name in ("claude-a", "claude-b")}
    for name, value in values.items():
        leases_api.env_secret(hub.client, hub.headers["owner"], name, VAR, value)
    theirs = sample("theirs-")
    leases_api.env_secret(hub.client, hub.headers["other"], "claude-theirs", VAR, theirs)

    listed = account_cli(monkeypatch, capsys, owner, "list", "--json")
    found = as_json(listed)
    assert_json_keys("hub account list", found)
    assert [(a["name"], a["state"], a["weight"]) for a in found] == [("claude-a", "ready", 1), ("claude-b", "ready", 1)]
    table = ok(account_cli(monkeypatch, capsys, owner, "list"))
    assert "claude-a" in table.out and "claude-theirs" not in table.out

    changed = account_cli(monkeypatch, capsys, owner, "set", "claude-b", "--weight", "3", "--priority", "2", "--json")
    account = as_json(changed)
    assert_json_keys("hub account set", account)
    assert (account["weight"], account["priority"]) == (3, 2)
    paused = ok(account_cli(monkeypatch, capsys, owner, "pause", "claude-a"))
    assert paused.out == f"Account claude-a ({VAR}): paused, weight 1, priority 0.\n"
    resumed = ok(account_cli(monkeypatch, capsys, owner, "resume", "claude-a"))
    assert resumed.out == f"Account claude-a ({VAR}): ready, weight 1, priority 0.\n"
    not_mine = account_cli(monkeypatch, capsys, owner, "pause", "claude-theirs")
    assert not_mine.code != 0

    default = ok(account_cli(monkeypatch, capsys, owner, "policy", PROJECT))
    assert default.out == (
        f"Project {PROJECT} (the defaults): strategy round-robin, cooldown 3600 s, failover on, at most 3 time(s).\n"
    )
    set_ = account_cli(
        monkeypatch, capsys, owner, "policy", PROJECT, "--strategy", "fill-first", "--no-failover", "--json"
    )
    policy = as_json(set_)
    assert_json_keys("hub account policy", policy)
    assert (policy["strategy"], policy["failover"], policy["cooldown_seconds"]) == ("fill-first", False, 3600)
    again = as_json(account_cli(monkeypatch, capsys, owner, "policy", PROJECT, "--cooldown", "600", "--json"))
    assert (again["strategy"], again["cooldown_seconds"]) == ("fill-first", 600)  # set over the policy as it is
    reset = as_json(account_cli(monkeypatch, capsys, owner, "policy", PROJECT, "--reset", "--json"))
    assert (reset["strategy"], reset["updated_at"]) == ("round-robin", None)
    theirs_policy = as_json(account_cli(monkeypatch, capsys, other, "policy", PROJECT, "--json"))
    assert theirs_policy["updated_at"] is None

    for result in (listed, table, changed, paused, resumed, not_mine, default, set_):
        nowhere(result, *values.values(), theirs)


def test_stdin_is_never_read(monkeypatch, capsys, tmp_path):
    result = run_cli(monkeypatch, capsys, tmp_path, "hub", "account", "set", "claude-a", stdin=io.StringIO("x"))
    assert result.code == 2
