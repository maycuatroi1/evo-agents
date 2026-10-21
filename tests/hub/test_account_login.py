"""``evo-agents hub account login``: a Claude account from ``claude setup-token``, added or renewed in one command.

The checks the claude-account-login plan names for its first step: the command runs ``claude setup-token`` (here a
stand-in on PATH that draws its screen as Claude Code does and prints a token drawn for the test) on a terminal of its
own, takes the token off that screen without the member copying it, and writes it as an env secret for
CLAUDE_CODE_OAUTH_TOKEN that ends a year from today less a day; it prints the account as ``account show`` does. With
``--paste``, with stdin not a terminal, or when the screen held no token, it reads the token or asks for it without
echo. Without ``claude`` on PATH it says how to do it by hand, and an API key is refused, pointing at
ANTHROPIC_API_KEY. Renewing an account replaces its token and its end alone: its projects and workers are sent again,
and its row in accounts (weight, priority, pause, cooldown) and its runs stay with the same secret id. The token is
never printed, not even in what the relay shows of claude setup-token's screen. ``account list`` and ``account show``
remind the member to renew an account that ends within 30 days.

What needs no hub runs without Postgres; everything that needs one skips without EVO_HUB_TEST_DSN."""

import io
import json
import os
import secrets
import sys
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from evo_agents.hub import setup_token
from evo_agents.hub.cli_secrets import Refused, login_ends, login_token
from evo_agents.hub.contract import commands
from evo_agents.hub.setup_token import Mask, Screen, default_end, find_token
from tests.hub import pg
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import HUB_URL, as_json, ok, write_credentials
from tests.hub.test_secrets_cli import needs_pg, nowhere, run_cli

VAR = "CLAUDE_CODE_OAUTH_TOKEN"
asked = secrets_cli.asked  # getpass as a person at a terminal answers it, as a fixture of this module

# A stand-in for `claude setup-token`: it records how it was started, draws its screen as Claude Code 2.1 does (a
# space is a jump to the next word's column), and prints the token it was given, wrapped by its box or by the margin.
FAKE_CLAUDE = r"""#!{python}
import json, os, sys, time

record = {{"argv": sys.argv[1:], "tty": [os.isatty(0), os.isatty(1)], "cols": os.get_terminal_size(1).columns}}
with open(os.environ["FAKE_CLAUDE_RECORD"], "w") as file:
    json.dump(record, file)
style, token, cols = os.environ["FAKE_CLAUDE_STYLE"], os.environ.get("FAKE_CLAUDE_TOKEN", ""), record["cols"]


def row(text, col=2):
    line, at = "", col
    for word in text.split(" "):
        line += f"\x1b[{{at}}G{{word}}"
        at += len(word) + 1
    return line + "\r\n"


def show(text):
    sys.stdout.write(text)
    sys.stdout.flush()
    time.sleep(0.05)


show("\x1b[?2004h\x1b[?25l" + row("This will guide you through long-lived auth token setup."))
show(row("Browser didn't open? Use the url below to sign in") + row("https://example.org/sign-in") + "\x1b[2A")
if style == "none":
    show("\x1b[2K" + row("Sign-in was cancelled.") + "\x1b[?25h\x1b[?2004l")
    sys.exit(1)
done = "\x1b[2K" + row("\x1b[32m✓ Long-lived authentication token created successfully!\x1b[39m") + "\r\n"
done += row("Your OAuth token (valid for 1 year):")
width = cols - 1 if style == "box" else len(token)
text = "".join(f"\x1b[2G\x1b[33m{{token[at : at + width]}}\x1b[39m\r\n" for at in range(0, len(token), width))
cut = text.index("sk-a") + 4
show(done + text[:cut])
show(text[cut:] + "\r\n" + row("Store this token securely. You won't be able to see it again."))
show(row("Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>") + "\x1b[?25h\x1b[?2004l")
"""


def oauth_token() -> str:
    """A token shaped as claude setup-token's, drawn for this test (108 characters, so it wraps at 80 columns)."""
    return "sk-ant-oat01-" + secrets.token_urlsafe(72)[:93] + "AA"


class Keys(io.StringIO):
    """A terminal the member sits at: getpass asks there, and claude setup-token reads its keys, of which none come
    (the end of a pipe)."""

    def __init__(self):
        super().__init__()
        self.fd, write = os.pipe()
        os.close(write)

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.fd

    def read(self, *args):
        raise AssertionError("a terminal is not read: getpass asks")


@pytest.fixture
def keys():
    terminal = Keys()
    yield terminal
    os.close(terminal.fd)


@pytest.fixture
def claude(tmp_path, monkeypatch):
    """``claude`` on PATH, the stand-in: ``use(style, token)`` sets what it prints, ``record()`` says how it ran (None
    when it did not). The member's terminal is 80 columns wide."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "claude"
    executable.write_text(FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    executable.chmod(0o755)
    record = tmp_path / "claude-record.json"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(record))
    monkeypatch.setattr(setup_token, "terminal_size", lambda: (80, 24))

    def use(style: str, token: str = "") -> None:
        record.unlink(missing_ok=True)
        monkeypatch.setenv("FAKE_CLAUDE_STYLE", style)
        monkeypatch.setenv("FAKE_CLAUDE_TOKEN", token)

    def ran():
        return json.loads(record.read_text(encoding="utf-8")) if record.exists() else None

    use("box")
    return SimpleNamespace(use=use, record=ran)


def login_cli(monkeypatch, capsys, home, *args: str, stdin=None) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, home, "hub", "account", "login", *args, stdin=stdin)


def unseen(result: SimpleNamespace, *tokens: str) -> None:
    """None of ``tokens``, nor a part of one, is in what the command printed."""
    nowhere(result, *tokens)
    for token in tokens:
        for part in (token[:24], token[30:60], token[-24:]):
            assert part not in result.out and part not in result.err


# Without a hub


def test_login_is_in_the_contract_and_takes_no_token_on_its_command_line():
    login = commands()["hub account login"]
    assert [p["name"] for p in login["positionals"]] == ["name"]
    options = {o["flags"][0]: o for o in login["options"]}
    assert set(options) == {"--project", "--worker", "--expires", "--paste", "--json"}
    assert options["--project"]["repeatable"] and not options["--project"]["required"]
    assert options["--worker"]["repeatable"] and options["--paste"]["value"] is None
    assert login["json"]["schema"] == "Account" and "value" not in login["json"]["keys"]


def test_an_account_ends_a_year_on_less_a_day_at_midnight_utc():
    assert default_end(date(2026, 10, 14)) == date(2027, 10, 13)
    assert default_end(date(2028, 2, 29)) == date(2029, 2, 27)
    late = datetime(2026, 10, 14, 23, 30, tzinfo=UTC)
    assert login_ends(None, late) == "2027-10-13T00:00:00+00:00"
    assert login_ends("2027-01-31", late) == "2027-01-31T00:00:00+00:00"
    with pytest.raises(Refused, match="--expires 2026-10-14 is past already"):
        login_ends("2026-10-14", late)
    with pytest.raises(Refused, match="--expires takes a day as YYYY-MM-DD"):
        login_ends("2027-02-30", late)


def drawn(*rows: str, col: int = 2) -> str:
    """Rows as Claude Code draws them: each word placed by a jump to its column, rows ended by CR CR LF."""
    out = ""
    for text in rows:
        at = col
        for word in text.split(" "):
            out += f"\x1b[{at}G{word}"
            at += len(word) + 1
        out += "\r\r\n"
    return out


@pytest.mark.parametrize("style", ["box to the margin", "box", "margin", "redrawn", "one row"])
def test_the_token_is_read_off_the_screen_joined_where_it_wrapped(style):
    token = oauth_token()
    head = drawn("✓ Long-lived authentication token created successfully!", "", "Your OAuth token (valid for 1 year):")
    tail = "\r\r\n" + drawn("Store this token securely. You won't be able to see it again.")
    if style == "box to the margin":  # Claude Code 2.1.296: from column 1 up to the margin, the next part at column 1
        body = "".join(f"\x1b[2G\x1b[38;5;220m{token[at : at + 79]}\x1b[39m\r\r\n" for at in range(0, len(token), 79))
    elif style == "box":  # the box wraps it at column 79, short of the margin
        body = "".join(f"\x1b[2G\x1b[33m{token[at : at + 78]}\x1b[39m\r\r\n" for at in range(0, len(token), 78))
    elif style == "margin":  # one write the terminal wraps at the margin
        body = f"\x1b[2G\x1b[33m{token}\x1b[39m\r\r\n"
    elif style == "redrawn":  # a placeholder first, then the same rows again over it
        body = "\x1b[2Gwaiting\r\r\n\x1b[1A\x1b[2K" + "".join(
            f"\x1b[2G{token[at : at + 78]}\r\r\n" for at in range(0, len(token), 78)
        )
    else:  # wide enough for one row, with more after it on the row
        body = f"\x1b[2G{token}\x1b[112G(copy it now)\r\r\n"
    screen = Screen(160 if style == "one row" else 80, 24)
    screen.feed("\x1b]8;;https://example.org/sign-in\x07sign in\x1b]8;;\x07\r\n" + head + body + tail)
    assert find_token(screen.lines(), screen.cols) == token


@pytest.mark.parametrize(
    "screen_text",
    [
        drawn("Sign-in was cancelled."),
        drawn("Your OAuth token (valid for 1 year):") + "\x1b[2Gsk-ant-oat01-abcdefghij…\r\r\n",
        drawn("Your OAuth token (valid for 1 year):") + "\x1b[2Gsk-ant-short\r\r\n",
        drawn("Your OAuth token (valid for 1 year):") + f"\x1b[2Gsk-ant-oat01-{'a' * 66}\r\r\n\x1b[5G{'b' * 29}\r\r\n",
    ],
    ids=["no token", "cut short", "not a token", "next part out of place"],
)
def test_a_screen_without_a_whole_token_gives_none(screen_text):
    screen = Screen(80, 24)
    screen.feed(screen_text)
    assert find_token(screen.lines(), screen.cols) is None


def test_the_relay_shows_the_screen_up_to_the_token_and_only_modes_after_it():
    token = oauth_token()
    shown = bytearray()
    mask = Mask(shown.extend)
    mask.feed(b"\x1b[2GYour\x1b[7GOAuth\r\n\x1b[2G\x1b[33msk-a")
    assert bytes(shown) == b"\x1b[2GYour\x1b[7GOAuth\r\n\x1b[2G\x1b[33m"  # what may be the token's start is held
    mask.feed(token[4:50].encode())
    mask.feed(token[50:].encode() + b"\x1b[39m\r\n\x1b[2GStore\x1b[?25h\x1b[?20")
    mask.feed(b"04l")
    mask.close()
    assert bytes(shown).endswith(b"\x1b[39m\x1b[?25h\x1b[?2004l\x1b[0m\x1b[?25h\r\n")
    assert b"sk-a" not in shown and token[4:20].encode() not in shown and b"Store" not in shown


def test_a_pasted_value_is_one_token_and_never_an_api_key():
    token = oauth_token()
    assert login_token(f"  {token}\n") == token
    api_key = "sk-ant-api03-" + secrets.token_urlsafe(60)
    with pytest.raises(Refused) as refused:
        login_token(api_key)
    assert "ANTHROPIC_API_KEY" in str(refused.value) and api_key not in str(refused.value)
    for wrong, problem in (("", "no token"), (f"{token[:50]} {token[50:]}", "holds a space or a line break")):
        with pytest.raises(Refused, match=problem):
            login_token(wrong)


def test_without_claude_on_path_the_error_says_how_to_do_it_by_hand(monkeypatch, capsys, tmp_path, keys):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")  # never reached: refused before
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    result = login_cli(monkeypatch, capsys, home, "claude-a", "--project", "demo", stdin=keys)
    assert result.code == 1 and result.out == ""
    assert result.err.startswith("error: there is no `claude` on PATH to run `claude setup-token` with.")
    assert "`evo-agents hub account login claude-a --paste`" in result.err
    assert "`pbpaste | evo-agents hub account login claude-a`" in result.err


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        (("Bad_Name", "--project", "demo"), "'Bad_Name' is not a secret's name"),
        (("claude-a", "--project", "demo", "--expires", "2027-1-31"), "--expires takes a day as YYYY-MM-DD"),
        (("claude-a", "--project", "demo", "--expires", "2001-01-31"), "--expires 2001-01-31 is past already"),
    ],
)
def test_arguments_are_checked_before_claude_runs(monkeypatch, capsys, tmp_path, keys, claude, args, problem):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = login_cli(monkeypatch, capsys, home, *args, stdin=keys)
    assert result.code == 2 and result.out == "" and problem in result.err
    assert claude.record() is None


# Against a hub

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub import test_credentials_api as leases_api
    from tests.hub.live import sql
    from tests.hub.test_accounts_api import cool
    from tests.hub.test_runs import PROJECT, report

    # the hub of test_secrets_cli, as fixtures of this module
    app_key, hub = secrets_cli.app_key, secrets_cli.hub


def account_cli(hub, monkeypatch, capsys, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "account", *args)


def secret_of(hub, monkeypatch, capsys, name: str) -> dict:
    found = as_json(run_cli(monkeypatch, capsys, hub.homes["owner"], "hub", "secret", "list", "--json"))
    return next(secret for secret in found if secret["name"] == name)


def account_row(db, name: str) -> tuple:
    """The secret's id, its row in accounts and the runs that took it, as the database holds them."""
    s, a, ra = tables.secrets, tables.accounts, tables.run_accounts
    ((secret_id,),) = sql(db, select(s.c.id).where(s.c.name == name, s.c.deleted_at.is_(None)))
    row = sql(db, select(*[c for c in a.c if c.name != "secret_id"]).where(a.c.secret_id == secret_id))
    runs = sql(db, select(ra.c.run_id, ra.c.env_var, ra.c.how).where(ra.c.secret_id == secret_id))
    return secret_id, row, sorted(runs)


def value_a_run_gets(hub, worker: dict, name: str) -> tuple[int, str]:
    run_id = leases_api.step_run(hub.client, hub.headers["owner"], "evo-agents", worker)
    value = leases_api.by_name(leases_api.leased(hub.client, worker, run_id))[name]["value"]
    assert report(hub.client, worker, run_id, "failed", error="done").status_code == 200
    return run_id, value


@needs_pg
def test_login_adds_an_account_and_renews_its_token_keeping_the_rest(hub, monkeypatch, capsys, keys, claude, asked):
    worker = leases_api.worker_of(hub.client, hub.headers["owner"], "mac-mini")
    first, second, third = oauth_token(), oauth_token(), oauth_token()
    default = f"{setup_token.default_end(datetime.now(UTC).date())} 00:00 UTC"

    claude.use("box", first)
    args = ("claude-a", "--project", PROJECT, "--worker", "mac-mini")
    added = ok(login_cli(monkeypatch, capsys, hub.homes["owner"], *args, stdin=keys))
    assert claude.record() == {"argv": ["setup-token"], "tty": [True, True], "cols": 80}
    assert added.out.startswith(
        "Created account claude-a. The hub keeps its token sealed and never shows it again.\n"
        f"Account claude-a ({VAR}): ready.\n  Projects: {PROJECT}; workers: mac-mini.\n  Expires: {default}.\n"
    )
    assert f"Adding account claude-a ({VAR}) for project(s) {PROJECT}, on mac-mini; it ends {default}.\n" in added.err
    assert "Running `claude setup-token`" in added.err and "Took the token `claude setup-token` printed.\n" in added.err
    assert "\x1b[2GYour\x1b[7GOAuth" in added.err and "sk-ant-" not in added.err  # the screen, up to the token
    secret = secret_of(hub, monkeypatch, capsys, "claude-a")
    bound = (secret["kind"], secret["env_var"], secret["projects"], secret["workers"])
    assert bound == ("env", VAR, [PROJECT], ["mac-mini"])
    assert secret["expires_at"].startswith(f"{setup_token.default_end(datetime.now(UTC).date())}T00:00:00")
    run_id, value = value_a_run_gets(hub, worker, "claude-a")
    assert value == first

    ok(account_cli(hub, monkeypatch, capsys, "set", "claude-a", "--weight", "3", "--priority", "2"))
    ok(account_cli(hub, monkeypatch, capsys, "pause", "claude-a"))
    cool(hub.db, "claude-a", 30)
    before = account_row(hub.db, "claude-a")
    assert [(run, var) for run, var, _ in before[2]] == [(run_id, VAR)]

    pasted = ok(login_cli(monkeypatch, capsys, hub.homes["owner"], "claude-a", "--paste", stdin=io.StringIO(second)))
    assert pasted.err.startswith(
        f"claude-a is an account of yours already: this replaces its token, which ends {default}, for project(s) "
        f"{PROJECT}, on mac-mini. Its weight, priority, pause, cooldown and runs stay as they are.\n"
    )
    assert pasted.out.startswith("Replaced the token of account claude-a; its weight, priority, pause, cooldown and ")
    assert f"Account claude-a ({VAR}): paused since " in pasted.out and "  Weight 3, priority 2; " in pasted.out
    assert account_row(hub.db, "claude-a") == before
    kept = secret_of(hub, monkeypatch, capsys, "claude-a")
    assert (kept["projects"], kept["workers"], kept["expires_at"]) == ([PROJECT], ["mac-mini"], secret["expires_at"])

    claude.use("none")
    asked.answer.value = third
    args = ("claude-a", "--expires", "2099-01-31", "--json")
    renewed = login_cli(monkeypatch, capsys, hub.homes["owner"], *args, stdin=keys)
    account = as_json(renewed)
    assert_json_keys("hub account login", account)
    assert (account["state"], account["weight"], account["expires_at"][:10]) == ("paused", 3, "2099-01-31")
    assert "Found no token in what `claude setup-token` printed" in renewed.err and "cancelled." in renewed.err
    assert asked.prompts == ["Token of `claude setup-token` for account claude-a (not shown): "]
    assert account_row(hub.db, "claude-a") == before

    ok(account_cli(hub, monkeypatch, capsys, "resume", "claude-a"))
    a = tables.accounts
    sql(hub.db, a.update().values(cooldown_until=None).where(a.c.secret_id == before[0]))
    again, value = value_a_run_gets(hub, worker, "claude-a")
    assert value == third and account_row(hub.db, "claude-a")[0] == before[0]
    assert [run for run, _, _ in account_row(hub.db, "claude-a")[2]] == sorted([run_id, again])
    for result in (added, pasted, renewed):
        unseen(result, first, second, third)


@needs_pg
def test_what_cannot_be_an_account_is_refused_before_claude_runs(hub, monkeypatch, capsys, keys, claude):
    owner = hub.homes["owner"]
    leases_api.git_secret(hub.client, hub.headers["owner"], "gitlab-kb", "https://gitlab.example.org/g", "glpat-x1")
    leases_api.env_secret(hub.client, hub.headers["owner"], "openai", "OPENAI_API_KEY", "sk-proj-x1")
    leases_api.worker_of(hub.client, hub.headers["other"], "theirs")
    claude.use("box", oauth_token())
    for args, problem in [
        (("gitlab-kb",), f"your secret gitlab-kb is of kind git, not {VAR}: give the Claude account another name"),
        (("openai",), f"your secret openai sets OPENAI_API_KEY, not {VAR}"),
        (("claude-a",), "a new account needs --project, a project whose runs take it"),
        (
            ("claude-a", "--project", "nowhere"),
            "an account for project nowhere needs the writer role on it; you hold no",
        ),
        (
            ("claude-a", "--project", PROJECT, "--worker", "theirs"),
            "theirs is not a worker of yours that is not revoked",
        ),
    ]:
        result = login_cli(monkeypatch, capsys, owner, *args, stdin=keys)
        assert result.code == 2 and result.out == "", result.err
        assert result.err.startswith(f"error: {problem}"), result.err
        assert claude.record() is None

    api_key = "sk-ant-api03-" + secrets.token_urlsafe(60)
    refused = login_cli(monkeypatch, capsys, owner, "claude-a", "--project", PROJECT, stdin=io.StringIO(api_key))
    assert refused.code == 2 and "goes in ANTHROPIC_API_KEY" in refused.err
    nowhere(refused, api_key)
    names = [s["name"] for s in as_json(run_cli(monkeypatch, capsys, owner, "hub", "secret", "list", "--json"))]
    assert names == ["gitlab-kb", "openai"]


@needs_pg
def test_list_and_show_remind_to_renew_an_account_that_ends_within_30_days(hub, monkeypatch, capsys):
    soon = (datetime.now(UTC) + timedelta(days=10, hours=12)).isoformat()
    later = (datetime.now(UTC) + timedelta(days=100)).isoformat()
    for name, var, ends in (("claude-soon", VAR, soon), ("claude-later", VAR, later), ("api-soon", "API_TOKEN", soon)):
        leases_api.env_secret(hub.client, hub.headers["owner"], name, var, oauth_token(), expires_at=ends)
    leases_api.env_secret(hub.client, hub.headers["owner"], "claude-past", VAR, oauth_token(), expires_at=later)
    s = tables.secrets
    sql(hub.db, s.update().values(expires_at=s.c.expires_at - timedelta(days=101)).where(s.c.name == "claude-past"))

    listed = ok(account_cli(hub, monkeypatch, capsys, "list"))
    reminders = listed.out.split("\n\n", 1)[1].splitlines()
    when = f"{soon[:10]} {soon[11:16]} UTC"
    assert reminders[0].startswith(f"Account api-soon: expires {soon[:10]} ")
    assert reminders[0].endswith(
        ", in 10 day(s): renew it with `evo-agents hub secret set api-soon --kind env --env-var API_TOKEN ...`."
    )
    assert reminders[1].endswith(
        ", already past, so no run takes it: renew it with `evo-agents hub account login claude-past`."
    )
    assert reminders[2] == (
        f"Account claude-soon: expires {when}, in 10 day(s): renew it with `evo-agents hub account login claude-soon`."
    )
    assert len(reminders) == 3 and "claude-later" not in listed.out.split("\n\n", 1)[1]
    shown = ok(account_cli(hub, monkeypatch, capsys, "show", "claude-soon"))
    assert f"  Expires: {when}, in 10 day(s): renew it with `evo-agents hub account login claude-soon`.\n" in shown.out
    later_shown = ok(account_cli(hub, monkeypatch, capsys, "show", "claude-later"))
    assert f"  Expires: {later[:10]} {later[11:16]} UTC.\n" in later_shown.out
    assert as_json(account_cli(hub, monkeypatch, capsys, "list", "--json"))[0]["name"] == "api-soon"
