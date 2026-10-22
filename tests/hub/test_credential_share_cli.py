"""``evo-agents hub secret share|unshare`` and ``hub account share|unshare`` (``evo_agents.hub.cli_shares``), as
criterion a2 of plan credential-sharing names them: the commands and their options are in the contract, ``--json``
prints the keys of CredentialShare, ``hub secret list --json`` and ``hub account show --json`` gain ``shares``, a
subscription token without ``--ack-terms`` prints TERMS_WARNING whole and exits non-zero, and no command prints a
value.

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

import pytest

from evo_agents.hub.contract import commands
from evo_agents.hub.credential_shares import TERMS_WARNING
from tests.hub import pg
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import HUB_URL, as_json, write_credentials
from tests.hub.test_secrets_cli import needs_pg, nowhere, run_cli, sample

SHARE_OPTIONS = {
    "--login",
    "--unit",
    "--all",
    "--project",
    "--level",
    "--budget-usd",
    "--usage-ceiling",
    "--max-running",
    "--expires",
    "--ack-terms",
}


# Without a hub


def test_credential_share_cli_commands_are_in_the_contract():
    printed = commands()
    for noun in ("secret", "account"):
        share, unshare = printed[f"hub {noun} share"], printed[f"hub {noun} unshare"]
        assert {option["flags"][0] for option in share["options"]} >= SHARE_OPTIONS
        assert {option["flags"][0] for option in unshare["options"]} >= {"--share"}
        for command in (share, unshare):
            assert command["json"]["schema"] == "CredentialShare" and command["json"]["kind"] == "object"
        levels = {o["flags"][0]: o for o in share["options"]}["--level"]["choices"]
        assert levels == ["A0", "A1", "A2", "A3", "A4", "A5"]
    assert "shares" in printed["hub secret list"]["json"]["keys"]
    assert "shares" in printed["hub account show"]["json"]["keys"]


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        (("--all", "--budget-usd", "0"), "error: --budget-usd takes more than 0 and at most 1000000\n"),
        (("--all", "--usage-ceiling", "101"), "error: --usage-ceiling takes a percent from 1 to 100\n"),
        (("--all", "--expires", "2027-02-30"), "error: --expires takes a day as YYYY-MM-DD, such as 2027-01-31, not"),
    ],
)
def test_credential_share_cli_refuses_a_bound_out_of_range_before_the_hub_is_asked(
    monkeypatch, capsys, tmp_path, args, problem
):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = run_cli(monkeypatch, capsys, home, "hub", "secret", "share", "notes-key", "--project", "demo", *args)
    assert result.code == 2 and result.out == "" and result.err.startswith(problem)


def test_credential_share_cli_takes_exactly_one_grantee(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    base = ("hub", "account", "share", "claude-a", "--project", "demo")
    for extra in ((), ("--all", "--login", "someone-else")):
        with pytest.raises(SystemExit) as stopped:
            run_cli(monkeypatch, capsys, home, *base, *extra)
        assert stopped.value.code == 2


# Against a hub

if pg.DSN:
    app_key, hub = secrets_cli.app_key, secrets_cli.hub


@needs_pg
def test_credential_share_cli_terms_warning_without_ack_then_share_list_and_unshare(hub, monkeypatch, capsys):
    from tests.hub import test_credentials_api as leases_api
    from tests.hub.test_runs import OTHER, PROJECT

    token = sample("oauth-")
    leases_api.env_secret(hub.client, hub.headers["owner"], "claude-a", "CLAUDE_CODE_OAUTH_TOKEN", token)
    home = hub.homes["owner"]
    args = ("hub", "account", "share", "claude-a", "--login", OTHER, "--project", PROJECT)
    refused = run_cli(monkeypatch, capsys, home, *args)
    assert refused.code != 0 and refused.out == ""
    assert refused.err.startswith(TERMS_WARNING + "\n") and "--ack-terms" in refused.err
    nowhere(refused, token)

    flags = ("--level", "A2", "--budget-usd", "20", "--usage-ceiling", "80", "--max-running", "2")
    made = run_cli(monkeypatch, capsys, home, *args, *flags, "--expires", "2099-01-31", "--ack-terms", "--json")
    shared = as_json(made)
    assert_json_keys("hub account share", shared)
    assert (shared["grantee"], shared["projects"], shared["level"], shared["budget_usd"]) == (
        OTHER,
        [PROJECT],
        "A2",
        20.0,
    )
    assert shared["terms_ack_at"] is not None and shared["expires_at"].startswith("2099-01-31T00:00:00")
    nowhere(made, token)

    listed = as_json(run_cli(monkeypatch, capsys, home, "hub", "secret", "list", "--json"))
    assert_json_keys("hub secret list", listed)
    assert [s["id"] for s in listed[0]["shares"]] == [shared["id"]]
    account = as_json(run_cli(monkeypatch, capsys, home, "hub", "account", "show", "claude-a", "--json"))
    assert_json_keys("hub account show", account)
    assert [s["id"] for s in account["shares"]] == [shared["id"]]

    text = run_cli(
        monkeypatch, capsys, home, "hub", "secret", "share", "claude-a", "--all", "--project", PROJECT, "--ack-terms"
    )
    assert text.code == 0 and text.out.startswith(f"Shared secret claude-a with everyone on the hub for {PROJECT}")
    other = run_cli(monkeypatch, capsys, hub.homes["other"], "hub", "secret", "unshare", "--share", str(shared["id"]))
    assert other.code != 0 and "no share" in other.err
    revoked = as_json(
        run_cli(monkeypatch, capsys, home, "hub", "account", "unshare", "--share", str(shared["id"]), "--json")
    )
    assert_json_keys("hub account unshare", revoked)
    assert revoked["revoked_by"] is not None
    for result in (text, other):
        nowhere(result, token)
