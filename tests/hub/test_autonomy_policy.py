"""The autonomy policy of a project (``evo_agents.hub.autonomy``, ``evo_agents.hub.server.autonomy_policies`` and
``evo_agents.hub.cli_policy``), as criterion a2 of plan gitlab-merge-queue names it: an admin of the project writes
the policy whole as its next revision, with the keys and defaults the member approved on 2026-10-21 (no key caps a
diff or the merges of a day); every revision is kept, audited, read back and compared; a key a revision leaves out
reads as its default; a project with no policy has every repo off. Its proposals by @builder are in
``tests.hub.test_builder``.

The model and the command line's parsing run without Postgres; everything that needs the hub skips without
EVO_HUB_TEST_DSN."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evo_agents.hub import autonomy
from evo_agents.hub.autonomy import Key, RepoFacts
from evo_agents.hub.cli_policy import read_policy
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
SHOP, SITE, DOCS = "northwind-shop", "northwind-site", "lumen.docs"
ORIGINS = {
    SHOP: ("https://gitlab.northwind.example/northwind/shop.git", "main"),
    SITE: ("git@github.com:northwind/site.git", "main"),
    DOCS: ("https://gitlab.lumen.example/lumen/docs.git", "trunk"),
}
FACTS = {name: RepoFacts(origin, branch) for name, (origin, branch) in ORIGINS.items()}
POLICY = {
    "repos": {
        SHOP: {
            "mode": "auto",
            "target_branch": "dev",
            "jobs": {"uat_check": "uat-smoke", "prod_deploy": "deploy-prod", "rollback": "rollback-prod"},
        },
        DOCS: {"mode": "auto", "prod_release": "after_uat", "after_uat_hours": 2},
    },
    "merge": {"risk_paths": ["services/billing/**"]},
    "judge": {"worker": "ai-lab-2"},
}
POLICY_YAML = """\
repos:
  northwind-shop:
    mode: auto
    target_branch: dev
    jobs: {uat_check: uat-smoke, prod_deploy: deploy-prod}
  northwind-site:
    mode: off
merge:
  hold_until: uat_check
"""


def modes(effective: dict) -> dict:
    return {name: repo["mode"] for name, repo in effective["repos"].items()}


# The model


def test_autonomy_policy_has_the_keys_and_defaults_the_member_approved():
    defaults = {key.name: key.default for key in autonomy.KEYS}
    assert defaults == {
        "merge.opt_in_label": "evo-merge",
        "merge.risk_paths": [],
        "merge.hold_until": "merged",
        "merge.pipeline_minutes": 60,
        "judge.worker": None,
        "judge.model": None,
        "breaker.target_red_minutes": 30,
        "breaker.reverts_in_a_row": 2,
        "breaker.revert_rate_factor": 2,
        "breaker.revert_rate_max": 0.10,
        "breaker.baseline_min_merges": 10,
        "breaker.judge_misses_7d": 2,
        "audit.sample_rate": 0.10,
        # plan loop-engine: the Loop off, and no estimate and no budget (answer 1 of its checkpoint)
        "loop.enabled": False,
        "loop.worker": None,
        "loop.timezone": None,
        "loop.business.max_running": 2,
        "loop.development.max_running": 1,
        "loop.repo_max_running": 1,
        "loop.dispatches_per_hour": 6,
        "loop.max_items_per_hour": 30,
        "loop.stable_hours": 0,
        "loop.brainstorm.weekday": "monday",
        "loop.brainstorm.time": "09:00",
        "loop.require_sprint": False,
        "loop.caps.author.minutes": 60,
        "loop.caps.author.turns": 200,
        "loop.caps.plan.minutes": 240,
        "loop.caps.plan.turns": 600,
        # plan problem-oriented-hub: a solution goes through use cases when the project keeps SRS documents
        "loop.use_cases": "auto",
    }
    assert not [name for name in defaults if "budget" in name or "estimate" in name or "usd" in name]
    assert {key.name: key.default for key in autonomy.REPO_KEYS} == {
        "mode": "off",
        "target_branch": None,
        "prod_release": "immediate",
        "after_uat_hours": 4,
        "jobs.uat_check": None,
        "jobs.prod_deploy": None,
        "jobs.prod_check": None,
        "jobs.rollback": None,
    }
    # no cap of a diff, and none of the merges of a day (answers 4 and 8)
    names = [key.name for key in (*autonomy.KEYS, *autonomy.REPO_KEYS)]
    assert not [name for name in names if "diff" in name or "per_day" in name or "max_merges" in name]
    # no policy: every repo of the project off, its target branch the repo's default one
    nothing = autonomy.effective(None, FACTS)
    assert modes(nothing) == {DOCS: "off", SHOP: "off", SITE: "off"}
    assert (nothing["repos"][DOCS]["target_branch"], nothing["merge"]["hold_until"]) == ("trunk", "merged")
    assert autonomy.repo_policy(None, SHOP, FACTS[SHOP])["mode"] == "off"


def test_autonomy_policy_reads_a_key_a_revision_leaves_out_or_no_longer_takes_as_its_default(monkeypatch):
    assert autonomy.problems(POLICY, FACTS) == []
    made = autonomy.effective(POLICY, FACTS)
    assert modes(made) == {DOCS: "auto", SHOP: "auto", SITE: "off"}
    assert made["repos"][SHOP]["target_branch"] == "dev" and made["repos"][DOCS]["target_branch"] == "trunk"
    assert made["repos"][SHOP]["jobs"] == {
        "uat_check": "uat-smoke",
        "prod_deploy": "deploy-prod",
        "prod_check": None,
        "rollback": "rollback-prod",
    }
    assert (made["merge"]["hold_until"], made["merge"]["risk_paths"], made["judge"]) == (
        "merged",
        ["services/billing/**"],
        {"worker": "ai-lab-2", "model": None},
    )
    # an old revision: a value this release refuses, and a key it does not know, read as the defaults
    old = {"merge": {"hold_until": "soon", "max_diff_lines": 500}, "repos": {"retired-repo": {"mode": "auto"}}}
    read = autonomy.effective(old, FACTS)
    assert read["merge"]["hold_until"] == "merged" and "max_diff_lines" not in read["merge"]
    assert read["repos"]["retired-repo"]["mode"] == "auto"  # a repo the project left stays named, as written
    # a revision written before plan loop-engine reads with the Loop off and every loop key at its default
    assert made["loop"]["enabled"] is False and made["loop"]["caps"]["plan"] == {"minutes": 240, "turns": 600}
    on = autonomy.effective({**POLICY, "loop": {"enabled": True, "brainstorm": {"time": "07:30"}}}, FACTS)["loop"]
    assert (on["enabled"], on["brainstorm"], on["stable_hours"]) == (True, {"weekday": "monday", "time": "07:30"}, 0)
    # a key a later release adds reads its default in a revision written before it
    added = Key("later.max_tasks_per_day", 3, lambda value: None, "a key a later plan adds")
    monkeypatch.setattr(autonomy, "KEYS", (*autonomy.KEYS, added))
    assert autonomy.effective(POLICY, FACTS)["later"] == {"max_tasks_per_day": 3}


def test_autonomy_policy_problems_name_each_key_and_value_it_refuses():
    wrong = {
        "repos": {
            SITE: {"mode": "auto"},
            "ghost": {"mode": "off"},
            SHOP: {"mode": False, "jobs": {"deploy": "x"}, "after_uat_hours": -1},
        },
        "merge": {"hold_until": "soon", "max_diff_lines": 500, "pipeline_minutes": True, "risk_paths": "a/**"},
        "audit": {"sample_rate": 2},
        "judge": {"worker": " "},
        "loop": {"enabled": "yes", "brainstorm": {"time": "9am"}, "timezone": "Asia/Ho Chi Minh", "budget": 5},
        "later": {},
    }
    assert autonomy.problems(wrong, FACTS) == [
        f"repos.{SITE}.mode: {SITE} is on github.com, where the Curator's tiers decide what the hub merges; auto is "
        "for a repo on GitLab",
        f"repos.ghost: the project has no repo ghost (its repos: {DOCS}, {SHOP}, {SITE})",
        f"repos.{SHOP}.mode: one of off, auto, not false",
        f"repos.{SHOP}.jobs.deploy: no such key; repos.{SHOP}.jobs takes uat_check, prod_deploy, prod_check, rollback",
        f"repos.{SHOP}.after_uat_hours: a number from 0 to 720, not -1",
        'merge.hold_until: one of merged, uat_check, prod_check, not "soon"',
        "merge.max_diff_lines: no such key; merge takes opt_in_label, risk_paths, hold_until, pipeline_minutes",
        "merge.pipeline_minutes: a whole number from 1 to 1440, not true",
        'merge.risk_paths: a list of at most 100 globs, each of 1 to 500 characters, not "a/**"',
        "audit.sample_rate: a number from 0 to 1, not 2",
        'judge.worker: null or text of 1 to 100 characters on one line, no space at either end, not " "',
        'loop.enabled: true or false, not "yes"',
        'loop.brainstorm.time: a time of day as HH:MM, 00:00 to 23:59, not "9am"',
        'loop.timezone: null or a time zone name such as Asia/Ho_Chi_Minh, at most 64 characters, not "Asia/Ho Chi '
        'Minh"',
        "loop.budget: no such key; loop takes enabled, worker, timezone, business, development, repo_max_running, "
        "dispatches_per_hour, max_items_per_hour, stable_hours, brainstorm, require_sprint, caps, use_cases",
        "later: no such key; the policy takes repos, merge, judge, breaker, audit, loop",
    ]
    assert autonomy.problems(["merge"], FACTS) == [
        'the policy is an object of sections (repos, merge, judge, breaker, audit, loop), not ["merge"]'
    ]
    assert autonomy.problems({"merge": {"risk_paths": ["x" * 70_000]}}, FACTS)[0].startswith("the policy is 70")


def test_autonomy_policy_dotted_keys_name_a_repo_with_a_dot_and_a_change_drops_what_it_empties():
    assert autonomy.key_path("merge.hold_until") == ("merge", "hold_until")
    assert autonomy.key_path(f"repos.{DOCS}.jobs.uat_check") == ("repos", DOCS, "jobs", "uat_check")
    assert autonomy.key_path(f"repos.{DOCS}.mode") == ("repos", DOCS, "mode")
    for unknown in ("merge.max_diff_lines", "repos.mode", f"repos.{SHOP}", "loop.max_tasks", "repos"):
        assert autonomy.key_path(unknown) is None, unknown
    sets = {f"repos.{DOCS}.jobs.rollback": "rollback-docs", "merge.hold_until": "uat_check"}
    changed, problems = autonomy.with_changes(POLICY, sets, ["judge.worker", f"repos.{SHOP}.target_branch"])
    assert problems == []
    assert changed["repos"][DOCS]["jobs"] == {"rollback": "rollback-docs"}
    assert "judge" not in changed and "target_branch" not in changed["repos"][SHOP]
    assert POLICY["judge"] == {"worker": "ai-lab-2"}, "the body changed is a copy"
    assert autonomy.changes(autonomy.effective(POLICY, FACTS), autonomy.effective(changed, FACTS)) == [
        (f"repos.{DOCS}.jobs.rollback", None, "rollback-docs"),
        (f"repos.{SHOP}.target_branch", "dev", "main"),
        ("merge.hold_until", "merged", "uat_check"),
        ("judge.worker", "ai-lab-2", None),
    ]
    same, refused = autonomy.with_changes(POLICY, {"merge.max_diff_lines": 500}, ["repos.mode"])
    assert (same, refused) == (
        POLICY,
        ["merge.max_diff_lines: the policy has no such key", "repos.mode: the policy has no such key"],
    )
    assert autonomy.flat({"repos": {DOCS: {"jobs": {"rollback": "r"}}}}) == {f"repos.{DOCS}.jobs.rollback": "r"}


# The command line, without the hub


def test_autonomy_policy_commands_are_in_the_contract():
    printed = commands()
    for verb, schema in (
        ("show", "AutonomyPolicy"),
        ("set", "PolicyWritten"),
        ("history", "PolicyRevision"),
        ("diff", "PolicyComparison"),
    ):
        assert printed[f"hub policy {verb}"]["json"]["schema"] == schema
    options = {option["flags"][0]: option for option in printed["hub policy set"]["options"]}
    assert options["--file"]["required"] and options["--if-revision"]["value"] == "integer"
    assert [p["metavar"] for p in printed["hub policy diff"]["positionals"]] == ["FROM", "TO"]


def test_autonomy_policy_file_reads_off_as_a_word_and_refuses_what_is_no_policy(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(POLICY_YAML + "judge: {model: 2026-10-21}\naudit: {sample_rate: 0.2}\n", encoding="utf-8")
    body = read_policy(str(path))
    assert body["repos"]["northwind-site"]["mode"] == "off"  # YAML 1.1 reads a bare off as false
    assert (body["judge"]["model"], body["audit"]["sample_rate"]) == ("2026-10-21", 0.2)
    path.write_text("checks: true\nmode: no\n", encoding="utf-8")
    assert read_policy(str(path)) == {"checks": True, "mode": "no"}
    for text, said in (("- merge\n", "holds no object of sections"), ("merge: [\n", "is not YAML")):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match=said):
            read_policy(str(path))
    path.write_text("", encoding="utf-8")
    assert read_policy(str(path)) == {}


def test_autonomy_policy_set_names_a_file_that_does_not_read_before_asking_the_hub(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "owner", "evh_unused")
    missing = str(tmp_path / "missing.yaml")
    result = run_cli(monkeypatch, capsys, home, "hub", "policy", "set", "--file", missing, "--project", "northwind")
    assert result.code == 2 and result.err.startswith("error: ") and "missing.yaml" in result.err


# On the hub

if pg.DSN:
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import PROJECT, registration
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import audit_rows

GRANTS = (("owner", 701, "admin"), ("alice", 702, "writer"), ("reader", 703, "reader"))


def _registered(client, github) -> dict:
    """The project with the three repos of ORIGINS, owner its admin, alice a writer and reader a reader; the headers of
    each and of the hub admin."""
    admin = live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])
    body = registration(OPEN_SINKS)
    body["repos"] = [
        {"name": name, "path": name, "origin": origin, "default_branch": branch}
        for name, (origin, branch) in ORIGINS.items()
    ]
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=admin).status_code == 200
    headers = {"admin": admin}
    for login, github_id, role in GRANTS:
        token = live.sign_in(client, github, login, github_id)["token"]
        grant = {"role": role, "max_level": "internal"}
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin)
        assert granted.status_code == 200, granted.text
        headers[login] = live.bearer(token)
    return headers


@pytest.fixture
def hub(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield SimpleNamespace(client=client, headers=_registered(client, github))


def _put(hub, who: str, policy: dict, **extra):
    return hub.client.put(f"/v1/projects/{PROJECT}/policy", json={"policy": policy, **extra}, headers=hub.headers[who])


def _get(hub, who: str, rest: str = ""):
    response = hub.client.get(f"/v1/projects/{PROJECT}/policy{rest}", headers=hub.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


@needs_pg
def test_autonomy_policy_is_written_by_an_admin_as_revisions_kept_audited_and_compared(hub, hub_db):
    none = _get(hub, "reader")
    assert (none["revision"], none["policy"], none["created_by"]) == (0, {}, None)
    assert modes(none["effective"]) == {DOCS: "off", SHOP: "off", SITE: "off"}
    # a writer, a reader and the hub admin without a grant write nothing
    assert "needs the admin role on it; alice holds writer" in _put(hub, "alice", POLICY).json()["message"]
    assert _put(hub, "reader", POLICY).status_code == 403
    refused = hub.client.get(f"/v1/projects/{PROJECT}/policy", headers=hub.headers["admin"])
    assert refused.status_code == 403 and "needs a grant on it" in refused.json()["message"]
    # a wrong policy names each problem and writes nothing
    wrong = _put(hub, "owner", {"repos": {SITE: {"mode": "auto"}}, "merge": {"max_merges_per_day": 20}})
    assert wrong.status_code == 422, wrong.text
    assert wrong.json()["message"] == (
        f"repos.{SITE}.mode: {SITE} is on github.com, where the Curator's tiers decide what the hub merges; auto is "
        "for a repo on GitLab; merge.max_merges_per_day: no such key; merge takes opt_in_label, risk_paths, "
        "hold_until, pipeline_minutes; nothing changed"
    )
    assert _get(hub, "reader")["revision"] == 0
    # the first revision; the same policy again writes nothing
    first = _put(hub, "owner", POLICY, note="  Shop and docs merge   on their own ")
    assert first.status_code == 200, first.text
    written = first.json()
    assert (written["revision"], written["changed"], written["created_by"]) == (1, True, "owner")
    assert (written["policy"], written["note"]) == (POLICY, "Shop and docs merge on their own")
    assert modes(written["effective"]) == {DOCS: "auto", SHOP: "auto", SITE: "off"}
    again = _put(hub, "owner", POLICY).json()
    assert (again["revision"], again["changed"]) == (1, False)
    # the second one, while the policy is at revision 1 alone
    second_body = {"repos": {SHOP: {"mode": "auto"}}, "merge": {"hold_until": "uat_check"}}
    stale = _put(hub, "owner", second_body, if_revision=0)
    assert stale.status_code == 409
    assert stale.json()["message"] == f"the policy of project {PROJECT} is at revision 1, not 0; nothing changed"
    second = _put(hub, "owner", second_body, if_revision=1).json()
    assert (second["revision"], second["effective"]["repos"][SHOP]["target_branch"]) == (2, "main")
    # every revision kept, read back and compared
    listed = _get(hub, "reader", "/revisions")
    assert [item["revision"] for item in listed] == [2, 1]
    assert listed[0]["changed"] == [
        f"repos.{DOCS}.mode",
        f"repos.{DOCS}.prod_release",
        f"repos.{DOCS}.after_uat_hours",
        f"repos.{SHOP}.target_branch",
        f"repos.{SHOP}.jobs.uat_check",
        f"repos.{SHOP}.jobs.prod_deploy",
        f"repos.{SHOP}.jobs.rollback",
        "merge.risk_paths",
        "merge.hold_until",
        "judge.worker",
    ]
    assert listed[1]["note"] == "Shop and docs merge on their own" and f"repos.{SHOP}.mode" in listed[1]["changed"]
    assert _get(hub, "reader", "/revisions/1")["policy"] == POLICY
    assert _get(hub, "reader")["policy"] == second_body
    missing = hub.client.get(f"/v1/projects/{PROJECT}/policy/revisions/9", headers=hub.headers["reader"])
    assert (
        missing.status_code == 404 and missing.json()["message"] == f"the policy of project {PROJECT} has no revision 9"
    )
    compared = _get(hub, "reader", "/compare?from=1&to=2")
    assert (compared["from_revision"], compared["to_revision"], len(compared["changes"])) == (1, 2, 10)
    assert {"key": "merge.hold_until", "before": "merged", "after": "uat_check"} in compared["changes"]
    since_none = _get(hub, "reader", "/compare?from=0")
    assert since_none["to_revision"] == 2
    assert since_none["changes"] == [
        {"key": f"repos.{SHOP}.mode", "before": "off", "after": "auto"},
        {"key": "merge.hold_until", "before": "merged", "after": "uat_check"},
    ]
    # each revision audited, by its keys and never a value
    assert audit_rows(hub_db, "policy") == [
        ("policy.set", f"{PROJECT}/policy revision=1 keys={','.join(listed[1]['changed'])}", "owner", PROJECT),
        ("policy.set", f"{PROJECT}/policy revision=2 keys={','.join(listed[0]['changed'])}", "owner", PROJECT),
    ]
    a = tables.autonomy_policies
    assert live.sql(hub_db, select(func.count()).select_from(a)) == [(2,)]


@needs_pg
def test_autonomy_policy_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        import httpx

        with httpx.Client(base_url=url, timeout=10) as client:
            headers = _registered(client, github)
        homes = {
            login: write_credentials(tmp_path / login, url, login, headers[login]["Authorization"].split()[1])
            for login, _, _ in GRANTS
        }

        def policy(who: str, *args: str) -> SimpleNamespace:
            return run_cli(monkeypatch, capsys, homes[who], "hub", "policy", *args, "--project", PROJECT)

        assert ok(policy("reader", "show")).out.startswith(f"Project {PROJECT} has no policy: every repo is off")
        path = tmp_path / "policy.yaml"
        path.write_text(POLICY_YAML, encoding="utf-8")
        refused = policy("alice", "set", "--file", str(path))
        assert refused.code == 1 and "needs the admin role on it; alice holds writer" in refused.err
        written = ok(policy("owner", "set", "--file", str(path), "--note", "shop merges itself")).out
        assert written.splitlines() == [
            f"Wrote revision 1 of the policy of project {PROJECT}.",
            "Changed:",
            f"  repos.{SHOP}.mode: off -> auto",
            f"  repos.{SHOP}.target_branch: main -> dev",
            f"  repos.{SHOP}.jobs.uat_check: null -> uat-smoke",
            f"  repos.{SHOP}.jobs.prod_deploy: null -> deploy-prod",
            "  merge.hold_until: merged -> uat_check",
        ]
        assert "nothing written" in ok(policy("owner", "set", "--file", str(path))).out
        shown = ok(policy("reader", "show")).out.splitlines()
        assert shown[0].startswith(f"Policy of project {PROJECT}: revision 1, written by owner at ")
        assert shown[1] == "  note: shop merges itself"
        assert f"repos.{SITE}.mode: off" in shown and f"repos.{DOCS}.mode: off (default)" in shown
        assert "merge.hold_until: uat_check" in shown and "merge.pipeline_minutes: 60 (default)" in shown
        assert_json_keys("hub policy show", as_json(policy("reader", "show", "--json")))
        assert_json_keys("hub policy show", as_json(policy("reader", "show", "--revision", "1", "--json")))
        path.write_text("merge: {hold_until: prod_check}\n", encoding="utf-8")
        assert_json_keys("hub policy set", as_json(policy("owner", "set", "--file", str(path), "--json")))
        stale = policy("owner", "set", "--file", str(path), "--if-revision", "1")
        assert stale.code == 1 and "is at revision 2, not 1" in stale.err
        history = ok(policy("reader", "history")).out.splitlines()
        assert history[0].split() == ["REVISION", "WRITTEN", "(UTC)", "BY", "CHANGED", "NOTE"]
        assert history[1].startswith("2 ") and f"repos.{SHOP}.mode, repos.{SHOP}.target_branch, " in history[1]
        assert "shop merges itself" in history[2] and "+2 more" in history[2]
        assert_json_keys("hub policy history", as_json(policy("reader", "history", "--json")))
        assert ok(policy("reader", "diff", "1", "2")).out.splitlines()[1:] == [
            f"  repos.{SHOP}.mode: auto -> off",
            f"  repos.{SHOP}.target_branch: dev -> main",
            f"  repos.{SHOP}.jobs.uat_check: uat-smoke -> null",
            f"  repos.{SHOP}.jobs.prod_deploy: deploy-prod -> null",
            "  merge.hold_until: uat_check -> prod_check",
        ]
        assert "read the same" in ok(policy("reader", "diff", "2")).out
        assert_json_keys("hub policy diff", as_json(policy("reader", "diff", "0", "--json")))
