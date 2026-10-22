"""The commands of the autonomy levels (``evo_agents.hub.cli_autonomy``), as criteria a2 and a3 of plan autonomy-levels
name them: `hub autonomy show [--login] [--agent] [--project] [--repo] [--json]`, `hub autonomy set --level A0..A5
[--class Kn=mode]... [--reset]`, and `hub autonomy ceiling set` with `--class`, `--environment` and `--worker`, over
HTTP alone; a refusal of the hub prints its reason and exits 1, a wrong command line exits 2 before the hub.

The contract and the printing run without Postgres; the commands against a hub skip without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evo_agents.hub import cli_autonomy
from evo_agents.hub.autonomy_levels import CLASSES
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def flags(printed: dict, path: str) -> set[str]:
    return {flag for option in printed[path]["options"] for flag in option["flags"]}


def test_autonomy_level_show_and_set_are_in_the_contract_with_the_keys_of_the_hub():
    printed = commands()
    show = printed["hub autonomy show"]
    assert show["json"]["keys"] == ["principal", "project", "agent", "level", "classes", "environments", "workers"]
    assert flags(printed, "hub autonomy show") == {"--login", "--agent", "--project", "--repo", "--json"}
    assert flags(printed, "hub autonomy set") == {"--level", "--class", "--reset", "--json"}
    level = next(o for o in printed["hub autonomy set"]["options"] if o["flags"] == ["--level"])
    assert level["choices"] == ["A0", "A1", "A2", "A3", "A4", "A5"]
    ceiling = printed["hub autonomy ceiling set"]
    assert {"--class", "--environment", "--worker"} <= flags(printed, "hub autonomy ceiling set")
    assert {"classes", "environments", "workers"} <= set(ceiling["json"]["keys"])


def test_autonomy_level_show_prints_each_class_with_its_mode_and_source():
    classes = {klass: {"mode": "auto", "source": "pref:lena", "detail": "level A5 of pref:lena"} for klass in CLASSES}
    classes["K5"] = {"mode": "ask", "source": "policy:demo/shop", "detail": "K5=ask of policy:demo/shop"}
    view = {
        "principal": "lena",
        "project": "demo",
        "agent": "bot",
        "level": "A4",
        "classes": classes,
        "environments": None,
        "workers": ["w1"],
    }
    lines = cli_autonomy.show_lines(view)
    assert lines[0] == "lena running bot in demo: level A4"
    assert lines[2 + 4].split() == ["K5", "ask", "policy:demo/shop", "K5=ask", "of", "policy:demo/shop"]
    assert lines[-2:] == ["Environments: any", "Workers: w1"]


def test_autonomy_level_pref_overrides_read_from_the_command_line_in_class_order():
    assert cli_autonomy.overrides(["K10=deny", "k6=ASK"]) == {"K6": "ask", "K10": "deny"}
    assert list(cli_autonomy.overrides(["K10=deny", "K6=ask"])) == ["K6", "K10"]
    with pytest.raises(cli_autonomy.Usage, match="twice"):
        cli_autonomy.overrides(["K6=deny", "K6=ask"])
    with pytest.raises(cli_autonomy.Usage, match="never takes ask"):
        cli_autonomy.overrides(["K2=ask"])


# Against a hub

if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_run_stream import serving

PEOPLE = (("lena", 821), ("ivan", 822), ("mia", 823))


@pytest.fixture
def autonomy_cli(hub_db, tmp_path, github, monkeypatch, capsys):
    """A hub under uvicorn with the admin and PEOPLE signed in; ``run(who, *args)`` runs `evo-agents hub ARGS`."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        homes = {}
        with httpx.Client(base_url=url, timeout=10) as client:
            for login, github_id in ((live.ADMIN, 302), *PEOPLE):
                token = live.sign_in(client, github, login, github_id)["token"]
                homes[login] = write_credentials(tmp_path / login, url, login, token)

        def run(who: str, *args: str) -> SimpleNamespace:
            return run_cli(monkeypatch, capsys, homes[who], "hub", *args)

        yield run


def refused(result: SimpleNamespace) -> str:
    assert result.code == 1 and result.err.startswith("error: "), result.out + result.err
    return result.err


@needs_pg
def test_autonomy_level_show_set_and_ceiling_over_http(autonomy_cli):
    run, admin = autonomy_cli, live.ADMIN
    shown = as_json(run("mia", "autonomy", "show", "--json"))
    assert_json_keys("hub autonomy show", shown)
    assert (shown["principal"], shown["level"], shown["classes"]["K12"]["mode"]) == ("mia", "A5", "auto")
    written = as_json(run("mia", "autonomy", "set", "--level", "A2", "--class", "K6=deny", "--json"))
    assert_json_keys("hub autonomy set", written)
    assert (written["level"], written["classes"], written["revision"]) == ("A2", {"K6": "deny"}, 1)
    said = ok(run("mia", "autonomy", "set", "--class", "K10=deny")).out.splitlines()
    assert said[0] == "Your preference is A2 K6=deny K10=deny (was A2), revision 2."
    text = ok(run("mia", "autonomy", "show")).out.splitlines()
    assert text[0] == "mia: level A2"
    assert text[2 + 5].split()[:3] == ["K6", "deny", "pref:mia"]
    assert text[-2:] == ["Environments: any", "Workers: any"]
    reset = as_json(run("mia", "autonomy", "set", "--reset", "--json"))
    assert (reset["level"], reset["classes"]) == ("A5", {})
    # the ceiling of a unit, with classes and places
    ok(run(admin, "org", "unit", "create", "lab", "--parent", "root", "--kind", "team"))
    ok(run(admin, "org", "seat", "add", "lena", "--unit", "lab", "--role", "lead"))
    ceiling = ("autonomy", "ceiling", "set", "--unit", "lab", "--level", "A4", "--note", "a new team")
    places = ("--class", "K6=ask", "--environment", "host", "--environment", "docker/py", "--worker", "w1")
    lab = as_json(run(admin, *ceiling, *places, "--json"))
    assert_json_keys("hub autonomy ceiling set", lab)
    assert (lab["classes"], lab["environments"], lab["workers"]) == ({"K6": "ask"}, ["docker/py", "host"], ["w1"])
    said = ok(run(admin, *ceiling, "--class", "K6=ask")).out.strip()
    assert said == "Ceiling of unit:lab is A4 K6=ask (was A4), revision 2."
    lena = as_json(run(admin, "autonomy", "show", "--login", "lena", "--json"))
    assert (lena["level"], lena["classes"]["K6"]["source"], lena["environments"]) == ("A4", "unit:lab", None)
    # refusals of the hub exit 1, a wrong command line exits 2
    cohort = ("org", "unit", "create", "intern-2026", "--parent", "lab", "--kind", "cohort", "--ends-on", "2026-12-31")
    ok(run("lena", *cohort))
    above = ("autonomy", "ceiling", "set", "--unit", "intern-2026", "--level", "A3", "--note", "more")
    assert "K6=notify lies above their own effective mode ask" in refused(run("lena", *above))
    assert "lena is none" in refused(run("lena", "autonomy", "show", "--login", "mia"))
    wrong = run("mia", "autonomy", "set", "--class", "K1=ask")
    assert wrong.code == 2 and "never takes ask" in wrong.err
    assert run("mia", "autonomy", "set").code == 2
    assert run("mia", "autonomy", "show", "--agent", "bot").code == 2
