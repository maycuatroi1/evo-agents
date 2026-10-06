"""``evo-agents hub secret set|list|delete`` and ``evo-agents hub run credentials``, as a signed-in member runs them,
and the refusal ``hub run dispatch`` prints for a worker that takes runs dispatched from the web only.

The checks step 12 of the worker-credentials plan names: ``secret set`` has no flag that takes the value and reads it
from stdin when stdin is not a terminal, else asks for it with getpass; every argument is checked before the value is
read; no command prints a value, not even in an error; ``secret list --json`` and ``run credentials --json`` print the
keys the contract declares (seam hub-cli-v1); ``run credentials`` lists each lease of a run with its name, provider,
target, worker and times, revoked ones included, for the run's owner alone; and ``run dispatch``, ``run plan`` and
``run rerun`` print why a worker set to take runs dispatched from the web only refuses one dispatched with a token.

The parsing of arguments and values runs without Postgres; everything that needs the hub skips without
EVO_HUB_TEST_DSN."""

import io
import json
import secrets
import sys
from types import SimpleNamespace

import pytest

from evo_agents.cli import main
from evo_agents.hub import cli_secrets
from evo_agents.hub.cli_secrets import Refused, expires_at, read_value
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import HUB_URL, as_json, ok, write_credentials

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def sample(prefix: str) -> str:
    """A value drawn for this test, so that finding it anywhere cannot be a coincidence."""
    return prefix + secrets.token_hex(16)


class Unread(io.StringIO):
    """A stdin that is not a terminal and fails the test when read: the command was to stop before the value."""

    def read(self, *args):
        raise AssertionError("the value was read")


class Terminal(io.StringIO):
    """A stdin that is a terminal; the value comes from getpass, never from reading it."""

    def isatty(self) -> bool:
        return True

    def read(self, *args):
        raise AssertionError("a terminal is not read: getpass asks")


def run_cli(monkeypatch, capsys, home, *args: str, stdin=None) -> SimpleNamespace:
    """``evo-agents ARGS`` in this process, as the member whose credentials ``home`` holds, with ``stdin``."""
    with monkeypatch.context() as patched:
        patched.setenv("HOME", str(home))
        patched.setattr(sys, "stdin", Unread() if stdin is None else stdin)
        code = main(list(args))
    out, err = capsys.readouterr()
    return SimpleNamespace(code=code, out=out, err=err)


@pytest.fixture
def asked(monkeypatch):
    """getpass as a person at a terminal answers it: each prompt recorded, the answer the test sets."""
    prompts: list[str] = []
    answer = SimpleNamespace(value="")

    def getpass(prompt: str = "Password: ", stream=None) -> str:
        prompts.append(prompt)
        return answer.value

    monkeypatch.setattr(cli_secrets.getpass, "getpass", getpass)
    return SimpleNamespace(prompts=prompts, answer=answer)


# Without a hub


def test_set_takes_no_value_on_its_command_line():
    flags = {option["flags"][0] for option in commands()["hub secret set"]["options"]}
    assert flags == {"--kind", "--env-var", "--url-prefix", "--username", "--project", "--worker", "--expires"}
    positionals = [entry["name"] for entry in commands()["hub secret set"]["positionals"]]
    assert positionals == ["name"]
    for argv in (["--value", "x"], ["--secret", "x"], ["extra-word"]):
        with pytest.raises(SystemExit):
            main(["hub", "secret", "set", "n", "--kind", "env", "--env-var", "API", "--project", "p", *argv])


def test_the_value_comes_from_stdin_with_one_final_line_break_dropped():
    assert read_value("n", io.StringIO("glpat-abc\n")) == "glpat-abc"
    assert read_value("n", io.StringIO("glpat-abc\r\n")) == "glpat-abc"
    assert read_value("n", io.StringIO("glpat-abc")) == "glpat-abc"
    assert read_value("n", io.StringIO("-----BEGIN-----\nbody\n-----END-----\n\n")) == (
        "-----BEGIN-----\nbody\n-----END-----\n"
    )
    for empty in ("", "\n", "\r\n"):
        with pytest.raises(Refused, match="no value: pipe it on stdin"):
            read_value("n", io.StringIO(empty))


def test_at_a_terminal_the_value_is_asked_for_without_echo(asked):
    asked.answer.value = "typed-value"
    assert read_value("claude-oauth", Terminal()) == "typed-value"
    assert asked.prompts == ["Value of secret claude-oauth (not shown): "]
    asked.answer.value = ""
    with pytest.raises(Refused, match="no value"):
        read_value("claude-oauth", Terminal())


def test_expires_is_a_day_that_ends_at_midnight_utc():
    assert expires_at("2027-01-31") == "2027-01-31T00:00:00+00:00"
    for wrong in ("2027-1-31", "20270131", "2027-02-30", "31/01/2027", "2027-01-31T12:00"):
        with pytest.raises(Refused, match="--expires takes a day as YYYY-MM-DD"):
            expires_at(wrong)


@pytest.mark.parametrize(
    "args, problem",
    [
        (("Bad_Name", "--kind", "env", "--env-var", "API"), "'Bad_Name' is not a secret's name"),
        (("n", "--kind", "env"), "--kind env needs --env-var"),
        (("n", "--kind", "env", "--env-var", "API", "--url-prefix", "https://h"), "belong to --kind git, not env"),
        (("n", "--kind", "env", "--env-var", "API", "--username", "u"), "belong to --kind git, not env"),
        (("n", "--kind", "env", "--env-var", "PATH"), "--env-var PATH steers the shell, git or the worker"),
        (("n", "--kind", "env", "--env-var", "EVO_HUB_URL"), "--env-var EVO_HUB_URL steers the shell"),
        (("n", "--kind", "env", "--env-var", "api_key"), "--env-var 'api_key' is not a variable name"),
        (("n", "--kind", "git"), "--kind git needs --url-prefix"),
        (("n", "--kind", "git", "--url-prefix", "https://h", "--env-var", "API"), "--env-var belongs to --kind env"),
        (("n", "--kind", "env", "--env-var", "API", "--expires", "2027-02-30"), "--expires takes a day"),
    ],
)
def test_arguments_are_checked_before_the_value_is_read(monkeypatch, capsys, tmp_path, args, problem):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = run_cli(monkeypatch, capsys, home, "hub", "secret", "set", *args, "--project", "demo")
    assert result.code == 2 and result.out == ""
    assert result.err.startswith("error: ") and problem in result.err


def test_without_a_sign_in_the_value_is_not_asked_for(monkeypatch, capsys, tmp_path, asked):
    args = ("hub", "secret", "set", "n", "--kind", "env", "--env-var", "API", "--project", "demo")
    for stdin in (Unread(), Terminal()):
        result = run_cli(monkeypatch, capsys, tmp_path / "nobody", *args, stdin=stdin)
        assert result.code == 1
        assert result.err == "error: not signed in to a hub: run `evo-agents hub login --url URL`\n"
    assert asked.prompts == []


@pytest.mark.parametrize(
    "kind, value, problem",
    [
        ("env", "", "no value: pipe it on stdin"),
        ("git", "glpat-first-line\nglpat-second-line\n", "the value of a secret of kind git is one line"),
        ("env", "sk-" + "x" * 16384, "a secret's value is at most 16384"),
        ("env", "sk-before\x00after", "holds a NUL character"),
    ],
)
def test_a_value_the_hub_would_refuse_is_refused_without_being_shown(
    monkeypatch, capsys, tmp_path, kind, value, problem
):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")  # never reached: refused before
    target = ("--env-var", "API") if kind == "env" else ("--url-prefix", "https://gitlab.example.org/group")
    args = ("hub", "secret", "set", "n", "--kind", kind, *target, "--project", "demo")
    result = run_cli(monkeypatch, capsys, home, *args, stdin=io.StringIO(value))
    assert result.code == 2 and result.out == "" and problem in result.err
    for part in filter(None, value.replace("\x00", "\n").splitlines()):
        assert part not in result.err


def test_delete_checks_the_name_before_asking_the_hub(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", HUB_URL, "owner", "evh_unused")
    result = run_cli(monkeypatch, capsys, home, "hub", "secret", "delete", "../tokens")
    assert result.code == 2 and "'../tokens' is not a secret's name" in result.err


def test_the_commands_of_secrets_and_leases_are_in_the_contract():
    printed = commands()
    assert {"hub secret set", "hub secret list", "hub secret delete", "hub run credentials"} <= set(printed)
    options = {o["flags"][0]: o for o in printed["hub secret set"]["options"]}
    assert options["--kind"]["required"] and options["--kind"]["choices"] == ["env", "git"]
    assert options["--project"]["required"] and options["--project"]["repeatable"]
    assert options["--worker"]["repeatable"] and not options["--worker"]["required"]
    assert printed["hub secret set"]["json"] is None and printed["hub secret delete"]["json"] is None
    assert printed["hub secret list"]["json"]["schema"] == "Secret"
    assert "value" not in printed["hub secret list"]["json"]["keys"]
    assert printed["hub run credentials"]["json"] == {
        "kind": "array",
        "keys": ["id", "name", "provider", "kind", "target", "worker", "issued_at", "expires_at", "revoked_at"],
        "schema": "RunLease",
    }


# Against a hub

if pg.DSN:
    import httpx
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from evo_agents.hub.server.app import create_app
    from tests.hub import test_credentials_api as leases_api
    from tests.hub.test_credentials_api import GITLAB_KB, MINE, OTHER, OWNER, PLAN, STEPS
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import PROJECT


@pytest.fixture(scope="module")
def app_key():
    """The GitHub App's key pair for this module: the private PEM the hub signs with, the public one the fake checks."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, public.decode()


@pytest.fixture
def hub(hub_db, tmp_path, github, app_key):
    """The hub under uvicorn with its sealing key and GitHub App, project evo-agents and plan credentials-smoke of
    ``tests.hub.test_credentials_api``, and a home signed in to it for owner and someone-else."""
    app = create_app(leases_api.hub_config(hub_db, tmp_path, github, app_key))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = leases_api.members(client, github)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("other", OTHER))
        }
        yield SimpleNamespace(url=url, client=client, headers=headers, homes=homes, db=hub_db, github=github)


def secret_cli(hub, monkeypatch, capsys, who: str, *args: str, stdin=None) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "secret", *args, stdin=stdin)


def run_of(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "run", *args, "--project", PROJECT)


def nowhere(result: SimpleNamespace, *values: str) -> None:
    for value in values:
        assert value not in result.out and value not in result.err


@needs_pg
def test_set_list_and_delete_a_secret_without_ever_printing_its_value(hub, monkeypatch, capsys, asked):
    leases_api.worker_of(hub.client, hub.headers["owner"], "mac-mini")
    oauth, replaced, glpat = sample("oauth-"), sample("oauth-"), sample("glpat-")

    piped = secret_cli(
        hub,
        monkeypatch,
        capsys,
        "owner",
        "set",
        "claude-oauth",
        "--kind",
        "env",
        "--env-var",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "--project",
        PROJECT,
        stdin=io.StringIO(oauth + "\n"),
    )
    assert ok(piped).out == (
        "Created secret claude-oauth (env, CLAUDE_CODE_OAUTH_TOKEN) for project(s) evo-agents, on any worker of "
        "yours. The hub keeps the value sealed and never shows it again.\n"
    )
    assert piped.err == "" and asked.prompts == []

    asked.answer.value = glpat
    typed = secret_cli(
        hub,
        monkeypatch,
        capsys,
        "owner",
        "set",
        "gitlab-kb",
        "--kind",
        "git",
        "--url-prefix",
        "https://GitLab.m1ops.com/fis-gb-m1/m1-kb-docs.git/",
        "--worker",
        "mac-mini",
        "--expires",
        "2099-01-31",
        "--project",
        PROJECT,
        stdin=Terminal(),
    )
    assert ok(typed).out == (
        f"Created secret gitlab-kb (git, {GITLAB_KB} as oauth2) for project(s) evo-agents, on mac-mini; it ends "
        "2099-01-31 00:00 UTC. The hub keeps the value sealed and never shows it again.\n"
    )
    assert asked.prompts == ["Value of secret gitlab-kb (not shown): "]

    again = secret_cli(
        hub,
        monkeypatch,
        capsys,
        "owner",
        "set",
        "claude-oauth",
        "--kind",
        "env",
        "--env-var",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "--project",
        PROJECT,
        stdin=io.StringIO(replaced),
    )
    assert ok(again).out.startswith("Replaced secret claude-oauth (env, CLAUDE_CODE_OAUTH_TOKEN) for project(s) ")

    listed = as_json(secret_cli(hub, monkeypatch, capsys, "owner", "list", "--json"))
    assert_json_keys("hub secret list", listed)
    assert [(s["name"], s["kind"], s["env_var"], s["url_prefix"], s["username"]) for s in listed] == [
        ("claude-oauth", "env", "CLAUDE_CODE_OAUTH_TOKEN", None, None),
        ("gitlab-kb", "git", None, GITLAB_KB, "oauth2"),
    ]
    assert [(s["projects"], s["workers"]) for s in listed] == [([PROJECT], []), ([PROJECT], ["mac-mini"])]
    assert listed[0]["expires_at"] is None and listed[1]["expires_at"].startswith("2099-01-31T00:00:00")
    table = ok(secret_cli(hub, monkeypatch, capsys, "owner", "list"))
    lines = table.out.splitlines()
    assert lines[0].split() == ["NAME", "KIND", "TARGET", "PROJECTS", "WORKERS", "EXPIRES", "(UTC)", "UPDATED", "(UTC)"]
    assert lines[1].split()[:5] == ["claude-oauth", "env", "CLAUDE_CODE_OAUTH_TOKEN", PROJECT, "any"]
    assert lines[2].split()[:7] == ["gitlab-kb", "git", GITLAB_KB, "as", "oauth2", PROJECT, "mac-mini"]
    other = ok(secret_cli(hub, monkeypatch, capsys, "other", "list"))
    assert other.out == "You keep no secret on this hub; `evo-agents hub secret set` adds one.\n"
    for result in (piped, typed, again, table):
        nowhere(result, oauth, replaced, glpat)

    # the value stored is what was piped or typed, the final line break dropped: what a run's worker gets
    worker = leases_api.worker_of(hub.client, hub.headers["owner"], "desk")
    run_id = leases_api.step_run(hub.client, hub.headers["owner"], "evo-agents", worker)
    assert leases_api.by_name(leases_api.leased(hub.client, worker, run_id))["claude-oauth"]["value"] == replaced
    dump = live.table_dump(hub.db)
    assert oauth not in dump and replaced not in dump and glpat not in dump

    # the hub's refusals come back as its message, and never with the value
    refusals = (
        (("--worker", "nobodys"), "nobodys is not a worker of yours"),
        (("--project", "nope"), "nope"),
    )
    for extra, problem in refusals:
        args = ("set", "x-token", "--kind", "env", "--env-var", "X_TOKEN", "--project", PROJECT, *extra)
        refused = secret_cli(hub, monkeypatch, capsys, "owner", *args, stdin=io.StringIO(oauth))
        assert refused.code == 1 and refused.out == "" and problem in refused.err
        nowhere(refused, oauth)
    past = ("set", "x-token", "--kind", "env", "--env-var", "X_TOKEN", "--expires", "2001-01-01", "--project", PROJECT)
    late = secret_cli(hub, monkeypatch, capsys, "owner", *past, stdin=io.StringIO(oauth))
    assert late.code == 1 and "expires_at is past already" in late.err

    deleted = ok(secret_cli(hub, monkeypatch, capsys, "owner", "delete", "gitlab-kb"))
    assert deleted.out == (
        "Deleted secret gitlab-kb: its value and bindings are gone from the hub, and its leases still out are "
        "revoked. Revoke it where it was made as well.\n"
    )
    assert [s["name"] for s in as_json(secret_cli(hub, monkeypatch, capsys, "owner", "list", "--json"))] == [
        "claude-oauth"
    ]
    gone = secret_cli(hub, monkeypatch, capsys, "owner", "delete", "gitlab-kb")
    assert gone.code == 1 and gone.err.startswith("error: you have no secret named gitlab-kb on this hub")
    theirs = secret_cli(hub, monkeypatch, capsys, "other", "delete", "claude-oauth")
    assert theirs.code == 1 and "you have no secret named claude-oauth" in theirs.err


@needs_pg
def test_run_credentials_lists_each_lease_of_a_run_for_its_owner_alone(hub, monkeypatch, capsys):
    hub.github.install(MINE, "evo-agents")
    oauth, glpat = sample("oauth-"), sample("glpat-")
    leases_api.env_secret(hub.client, hub.headers["owner"], "claude-oauth", "CLAUDE_CODE_OAUTH_TOKEN", oauth)
    leases_api.git_secret(hub.client, hub.headers["owner"], "gitlab-kb", GITLAB_KB, glpat)
    worker = leases_api.worker_of(hub.client, hub.headers["owner"], "mac-mini")
    run_id = leases_api.plan_run(hub.client, hub.headers["owner"], worker)

    empty = ok(run_of(hub, monkeypatch, capsys, "owner", "credentials", str(run_id)))
    assert empty.out == f"Run #{run_id} got no lease: its worker asked for none, or the hub had nothing for it.\n"

    answer = leases_api.leased(hub.client, worker, run_id)
    token = leases_api.by_name(answer)[f"github-app:{MINE}"]["value"]
    printed = as_json(run_of(hub, monkeypatch, capsys, "owner", "credentials", str(run_id), "--json"))
    assert_json_keys("hub run credentials", printed)
    assert [(lease["name"], lease["provider"], lease["kind"], lease["target"]) for lease in printed] == [
        ("claude-oauth", "secret", "env", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("gitlab-kb", "secret", "git", GITLAB_KB),
        (f"github-app:{MINE}", "github-app", "git", f"https://github.com/{MINE}/evo-agents"),
    ]
    assert [lease["id"] for lease in printed] == sorted(lease["id"] for lease in answer["leases"])
    assert {lease["worker"] for lease in printed} == {"mac-mini"}
    assert all(lease["issued_at"] and lease["revoked_at"] is None for lease in printed)
    assert printed[0]["expires_at"] is None and printed[2]["expires_at"] is not None
    table = ok(run_of(hub, monkeypatch, capsys, "owner", "credentials", str(run_id)))
    lines = table.out.splitlines()
    header = ["NAME", "PROVIDER", "TARGET", "WORKER", "ISSUED", "(UTC)", "EXPIRES", "(UTC)", "STATE"]
    assert lines[0].split() == header
    assert lines[1].split()[:4] == ["claude-oauth", "secret", "CLAUDE_CODE_OAUTH_TOKEN", "mac-mini"]
    assert lines[1].split()[-1] == "out" and lines[3].split()[-1] == "out"
    assert lines[-1] == f"3 lease(s) of run #{run_id}, 3 still out."
    for result in (empty, table):
        nowhere(result, oauth, glpat, token)
    assert oauth not in json.dumps(printed) and glpat not in json.dumps(printed) and token not in json.dumps(printed)

    # given back, each shows when; the next ask leases them again, as new rows for the token
    assert leases_api.give_back(hub.client, worker["headers"], run_id).status_code == 200
    after = ok(run_of(hub, monkeypatch, capsys, "owner", "credentials", str(run_id)))
    states = [line.split()[-3:] for line in after.out.splitlines()[1:-1]]
    assert [state[0] for state in states] == ["revoked"] * 3
    assert after.out.splitlines()[-1] == (
        f"3 lease(s) of run #{run_id}, none still out: each was given back, taken back or ended."
    )
    revoked = as_json(run_of(hub, monkeypatch, capsys, "owner", "credentials", str(run_id), "--json"))
    assert all(lease["revoked_at"] for lease in revoked)

    other = run_of(hub, monkeypatch, capsys, "other", "credentials", str(run_id))
    assert other.code == 1 and other.err == (
        f"error: only {OWNER}, who dispatched run {run_id}, sees the credentials it got\n"
    )
    missing = run_of(hub, monkeypatch, capsys, "owner", "credentials", str(run_id + 100))
    assert missing.code == 1 and f"project {PROJECT} has no run {run_id + 100}" in missing.err


@needs_pg
def test_dispatch_plan_and_rerun_print_why_a_worker_of_the_web_refuses_a_token(hub, monkeypatch, capsys):
    worker = leases_api.worker_of(hub.client, hub.headers["owner"], "mac-mini")
    pinned = ("dispatch", PLAN, str(STEPS["notes"]), "--worker", "mac-mini", "--json")
    queued = as_json(run_of(hub, monkeypatch, capsys, "owner", *pinned))
    ok(run_of(hub, monkeypatch, capsys, "owner", "cancel", str(queued[0]["id"])))
    live.sql(hub.db, "UPDATE workers SET dispatch_from = 'web' WHERE id = %s", (worker["id"],))

    why = (
        "error: worker mac-mini takes only runs dispatched from a web session, as its owner set it, so a token cannot "
        "hand it work: dispatch on the web, or to another worker; nothing was dispatched\n"
    )
    for args in (
        ("dispatch", PLAN, str(STEPS["evo-agents"]), "--worker", "mac-mini"),
        ("dispatch", PLAN, str(STEPS["evo-agents"]), "--worker", str(worker["id"])),
        ("plan", PLAN, "--worker", "mac-mini"),
        ("rerun", str(queued[0]["id"])),
    ):
        refused = run_of(hub, monkeypatch, capsys, "owner", *args)
        assert (refused.code, refused.out, refused.err) == (1, "", why), args
    listed = as_json(run_of(hub, monkeypatch, capsys, "owner", "list", "--json"))
    assert [run["id"] for run in listed["runs"]] == [queued[0]["id"]]  # nothing was queued
