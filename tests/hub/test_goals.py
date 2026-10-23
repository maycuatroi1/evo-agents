"""The goals of a project (``evo_agents.hub.goals``, ``evo_agents.hub.server.goals`` and ``evo_agents.hub.cli_goals``),
as criterion a2 of plan loop-engine names it: a goal has a statement, a key of the project's signals as its metric with
a target, a deadline, the canvas it drives, an owner and a status, and no budget; an admin of the project writes it
whole as its next revision, every revision is kept and audited; ``evo-agents hub goal list|show|set|close``. The
Curator reads the goals of the project, and a goal of an older charter (``{id, what}``) becomes a goal without a
metric when the goals are read, its id kept as the alias. Proposals of a goal by @builder are in
``tests.hub.test_builder``.

The model and the command line's parsing run without Postgres; everything that needs the hub skips without
EVO_HUB_TEST_DSN."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import goals, tables
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
METRIC = {"key": "search.zero_rate", "aggregate": "p50", "window_days": 30, "comparator": "<=", "target": 0.05}


# The model


def test_goal_codes_are_never_aliases_and_a_goal_reads_with_its_metric():
    assert (goals.code(7), goals.number_of("G-7"), goals.number_of("night-shift"), goals.number_of("g-7")) == (
        "G-7",
        7,
        None,
        None,
    )
    for ref in ("G-1", "G-123456789", "night-shift", "g-1", "3"):
        assert re.fullmatch(goals.REF, ref), ref
    for ref in ("G-0", "G-01", "Night-Shift", "-shift", "G-1234567890", ""):
        assert not re.fullmatch(goals.REF, ref), ref
    assert goals.metric_text(None) == "no metric"
    assert goals.metric_text(METRIC) == "search.zero_rate p50 over 30 days <= 0.05"
    assert goals.metric_text({**METRIC, "window_days": None, "tenant": "lumen"}) == (
        "search.zero_rate p50 over 90 days <= 0.05 for tenant lumen"
    )
    line = goals.curator_line(
        {"code": "G-2", "alias": "search", "statement": " Fewer   searches\nfind nothing ", "metric": METRIC}
        | {"deadline": "2026-12-31", "canvas": "northwind"}
    )
    assert line == {
        "id": "G-2 (search)",
        "what": "Fewer searches find nothing; reached when search.zero_rate p50 over 30 days <= 0.05; by 2026-12-31; "
        "drives canvas northwind",
    }
    assert goals.changed({"rank": 1, "metric": None}, {"rank": 2, "status": "active"}) == ["rank", "status"]
    assert not [name for name in goals.FIELDS if "budget" in name]  # a goal has no budget (checkpoint of step 1)


def test_goal_commands_are_in_the_contract():
    printed = commands()
    for verb, schema in (("list", "ProjectGoal"), ("show", "ProjectGoal"), ("set", "ProjectGoalWritten")):
        assert printed[f"hub goal {verb}"]["json"]["schema"] == schema
    assert printed["hub goal close"]["json"]["schema"] == "ProjectGoalWritten"
    options = {option["flags"][0]: option for option in printed["hub goal set"]["options"]}
    assert options["--aggregate"]["choices"] == list(goals.AGGREGATES)
    assert options["--status"]["choices"] == ["active", "paused"] and options["--target"]["value"] == "number"
    assert [p["required"] for p in printed["hub goal set"]["positionals"]] == [False]
    close = {option["flags"][0]: option for option in printed["hub goal close"]["options"]}
    assert close["--reason"]["required"] and close["--reason"]["choices"] == list(goals.CLOSE_REASONS)


def test_goal_set_names_a_metric_it_cannot_make_before_asking_the_hub(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "owner", "evh_unused")

    def goal(*args: str) -> SimpleNamespace:
        return run_cli(monkeypatch, capsys, home, "hub", "goal", "set", *args, "--project", "northwind")

    result = goal("--statement", "Fewer searches find nothing", "--metric", "search.zero_rate")
    assert result.code == 2 and result.err == "error: a metric needs --aggregate, --comparator, --target\n"
    result = goal("G-1", "--no-metric", "--target", "0.1")
    assert result.code == 2 and "--no-metric goes without the options of a metric" in result.err
    result = goal("G-1", "--deadline", "2026-12-31", "--no-deadline")
    assert result.code == 2 and "--no-deadline, --no-canvas and --no-problem each go without" in result.err
    result = goal("G-1", "--problem", "P-1", "--no-problem")
    assert result.code == 2 and "--no-deadline, --no-canvas and --no-problem each go without" in result.err


# On the hub

if pg.DSN:
    from sqlalchemy import func, insert, select

    from tests.hub.test_autonomy_policy import GRANTS, _registered, hub  # noqa: F401 (hub is a fixture)
    from tests.hub.test_curator import NIGHT, OWNER, charter_body, night, written  # noqa: F401 (night is a fixture)
    from tests.hub.test_plans import PROJECT
    from tests.hub.test_review_runs import collected, review  # noqa: F401 (review is a fixture)
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import audit_rows, claim, web_client  # noqa: F401 (web_client is a fixture)


def _signal_keys(client, headers) -> None:
    """The registry of the project: a key of kind number, one of kind feedback, and one archived."""
    for name, kind in (("search.zero_rate", "number"), ("support.tickets", "feedback"), ("search.old_rate", "number")):
        made = client.post(f"/v1/projects/{PROJECT}/signal-keys", json={"name": name, "kind": kind}, headers=headers)
        assert made.status_code == 201, made.text


def _canvas(db, slug: str, *, archived: bool = False) -> None:
    u = tables.users
    owner = live.sql(db, select(u.c.id).where(u.c.login == "owner"))[0][0]
    project = live.sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    row = {"project_id": project, "slug": slug, "name": slug.title(), "label": {}, "created_by": owner}
    if archived:
        row |= {"archived_by": owner, "archived_at": func.now()}
    live.sql(db, insert(tables.canvas.canvases).values(row))


def _put(hub, who: str, ref: str, body: dict):
    return hub.client.put(f"/v1/projects/{PROJECT}/goals/{ref}", json=body, headers=hub.headers[who])


def _close(hub, who: str, ref: str, body: dict):
    return hub.client.post(f"/v1/projects/{PROJECT}/goals/{ref}/close", json=body, headers=hub.headers[who])


def _get(hub, who: str, rest: str = ""):
    response = hub.client.get(f"/v1/projects/{PROJECT}/goals{rest}", headers=hub.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def _written(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


@needs_pg
def test_goals_are_written_by_an_admin_as_revisions_kept_audited_and_listed_by_rank(hub, hub_db):
    _signal_keys(hub.client, hub.headers["owner"])
    _canvas(hub_db, "northwind")
    assert _get(hub, "reader") == []
    # a writer, a reader and the hub admin without a grant write nothing; the hub admin reads nothing
    first_body = {"statement": "  Fewer searches find nothing ", "metric": METRIC, "deadline": "2026-12-31"}
    first_body |= {"canvas": "northwind", "note": " zero   results first "}
    assert "needs the admin role on it; alice holds writer" in _put(hub, "alice", "G-1", first_body).json()["message"]
    assert _put(hub, "reader", "G-1", first_body).status_code == 403
    refused = hub.client.get(f"/v1/projects/{PROJECT}/goals", headers=hub.headers["admin"])
    assert refused.status_code == 403 and "needs a grant on it" in refused.json()["message"]
    # the first goal, ranked first and owned by its writer; the same goal again writes nothing
    first = _written(_put(hub, "owner", "G-1", first_body))
    assert (first["code"], first["number"], first["alias"], first["revision"], first["changed"]) == (
        "G-1",
        1,
        None,
        1,
        True,
    )
    assert (first["statement"], first["rank"], first["owner"], first["status"], first["created_by"]) == (
        "Fewer searches find nothing",
        1,
        "owner",
        "active",
        "owner",
    )
    assert first["metric"] == {**METRIC, "tenant": None}
    assert (first["deadline"], first["canvas"], first["note"], first["close_reason"]) == (
        "2026-12-31",
        "northwind",
        "zero results first",
        None,
    )
    again = _written(_put(hub, "owner", "G-1", first_body))
    assert (again["revision"], again["changed"]) == (1, False)
    # a new goal takes the next code, never an alias
    skipped = _put(hub, "owner", "G-3", {"statement": "Answer every ticket in a day"})
    assert skipped.status_code == 422
    assert skipped.json()["message"] == f"the next goal of project {PROJECT} is G-2, not G-3; nothing changed"
    alias = _put(hub, "owner", "tickets", {"statement": "Answer every ticket in a day"})
    assert alias.status_code == 404
    assert alias.json()["message"] == f"project {PROJECT} has no goal tickets; a new goal takes the code G-2"
    second = _written(_put(hub, "owner", "G-2", {"statement": "Answer every ticket in a day", "owner": "alice"}))
    assert (second["rank"], second["owner"], second["metric"], second["deadline"]) == (2, "alice", None, None)
    # the next revision of the first goal, while it is at revision 1 alone
    later = {**first_body, "metric": None, "rank": 3, "status": "paused"}
    stale = _put(hub, "owner", "G-1", {**later, "if_revision": 0})
    assert stale.status_code == 409
    assert stale.json()["message"] == f"goal G-1 of project {PROJECT} is at revision 1, not 0; nothing changed"
    paused = _written(_put(hub, "owner", "G-1", {**later, "if_revision": 1}))
    assert (paused["revision"], paused["status"], paused["rank"], paused["metric"]) == (2, "paused", 3, None)
    assert paused["owner"] == "owner", "the owner is kept when the body leaves it out"
    # every revision kept and read back
    revisions = _get(hub, "reader", "/G-1/revisions")
    assert [(item["revision"], item["status"], item["changed"]) for item in revisions] == [
        (2, "paused", ["metric", "rank", "status"]),
        (1, "active", ["statement", "metric", "deadline", "canvas", "rank", "owner", "status"]),
    ]
    assert _get(hub, "reader", "/G-1?revision=1")["metric"] == {**METRIC, "tenant": None}
    missing = hub.client.get(f"/v1/projects/{PROJECT}/goals/G-1?revision=9", headers=hub.headers["reader"])
    assert missing.status_code == 404 and missing.json()["message"] == f"project {PROJECT} has no goal G-1 revision 9"
    assert [goal["code"] for goal in _get(hub, "reader")] == ["G-2", "G-1"]  # by rank
    assert [goal["code"] for goal in _get(hub, "reader", "?status=paused")] == ["G-1"]
    # closing is a revision too, once
    closed = _written(_close(hub, "owner", "G-2", {"reason": "achieved", "note": "a day, every day"}))
    assert (closed["revision"], closed["status"], closed["close_reason"]) == (2, "closed", "achieved")
    twice = _close(hub, "owner", "G-2", {"reason": "dropped"})
    assert twice.status_code == 409 and "is closed already (achieved)" in twice.json()["message"]
    assert _close(hub, "alice", "G-1", {"reason": "dropped"}).status_code == 403
    # each revision audited, by its fields and never a value
    assert audit_rows(hub_db, "goal") == [
        (
            "goal.set",
            f"{PROJECT}/goal G-1 revision=1 fields=statement,metric,deadline,canvas,rank,owner,status",
            "owner",
            PROJECT,
        ),
        ("goal.set", f"{PROJECT}/goal G-2 revision=1 fields=statement,rank,owner,status", "owner", PROJECT),
        ("goal.set", f"{PROJECT}/goal G-1 revision=2 fields=metric,rank,status", "owner", PROJECT),
        ("goal.set", f"{PROJECT}/goal G-2 revision=2 fields=status,close_reason", "owner", PROJECT),
    ]
    assert live.sql(hub_db, select(tables.goal_revisions.c.revision).order_by(tables.goal_revisions.c.revision)) == [
        (1,),
        (1,),
        (2,),
        (2,),
    ]


@needs_pg
def test_goal_writes_name_each_problem_of_their_metric_canvas_and_owner(hub, hub_db):
    _signal_keys(hub.client, hub.headers["owner"])
    _canvas(hub_db, "northwind")
    _canvas(hub_db, "retired", archived=True)
    wrong = {"statement": "Fewer searches find nothing", "metric": {**METRIC, "key": "search.ghost_rate"}}
    refused = _put(hub, "owner", "G-1", {**wrong, "canvas": "lumen", "owner": "reader"})
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"] == (
        f"owner: reader holds reader on project {PROJECT}; a goal's owner holds the writer role or above; canvas: "
        f"project {PROJECT} has no canvas lumen; metric.key: the registry of project {PROJECT} has no key "
        "search.ghost_rate: an admin adds it with `evo-agents hub signal keys add`; nothing changed"
    )
    for changes, said in (
        ({"metric": {**METRIC, "key": "support.tickets"}}, "support.tickets is a key of kind feedback; a goal's"),
        ({"canvas": "retired"}, "canvas: canvas retired is archived; nothing changed"),
        ({"owner": "ghost"}, f"owner: ghost holds no grant on project {PROJECT}"),
    ):
        response = _put(hub, "owner", "G-1", {"statement": "Fewer searches find nothing", **changes})
        assert response.status_code == 422 and said in response.json()["message"], response.text
    for shape in (
        {"statement": "   "},
        {"statement": "x", "status": "closed"},  # a closed goal names its reason
        {"statement": "x", "close_reason": "achieved"},  # and only a closed one does
        {"statement": "x", "budget_usd": 50},  # a goal has no budget
        {"statement": "x", "metric": {**METRIC, "aggregate": "max"}},
        {"statement": "x", "metric": {**METRIC, "window_days": 91}},
        {"statement": "x" * 2001},
    ):
        assert _put(hub, "owner", "G-1", shape).status_code == 422, shape
    assert _get(hub, "reader") == []
    # a key or a canvas archived since a revision named it may stay
    kept = _written(_put(hub, "owner", "G-1", {"statement": "x", "metric": {**METRIC, "key": "search.old_rate"}}))
    archived = hub.client.post(
        f"/v1/projects/{PROJECT}/signal-keys/search.old_rate/archive", headers=hub.headers["owner"]
    )
    assert archived.status_code == 200, archived.text
    body = {"statement": "Fewer old searches", "metric": kept["metric"]}
    assert _written(_put(hub, "owner", "G-1", body))["revision"] == 2
    response = _put(hub, "owner", "G-2", {"statement": "y", "metric": kept["metric"]})
    assert response.status_code == 422 and "metric.key: search.old_rate is archived" in response.json()["message"]


@needs_pg
def test_curator_charter_goals_become_goals_once_and_the_review_prompt_lists_the_active_goals(review, hub_db):
    client, headers = review.client, review.headers
    goals_path = f"/v1/projects/{PROJECT}/goals"
    # the charter's goal night-shift becomes G-1 when the goals are read, once
    listed = client.get(goals_path, headers=headers["reader"]).json()
    assert [(g["code"], g["alias"], g["metric"], g["rank"], g["owner"], g["created_by"]) for g in listed] == [
        ("G-1", "night-shift", None, 1, OWNER, OWNER)
    ]
    assert listed[0]["statement"] == "Run the plans the owner approved while the owner sleeps."
    assert listed[0]["note"] == "made from goal night-shift of the Curator's charter, revision 1"
    assert client.get(goals_path, headers=headers["owner"]).json() == listed
    a = tables.audit
    made = live.sql(hub_db, select(a.c.actor_id, a.c.target).where(a.c.action == "goal.set"))
    assert made == [(None, f"{PROJECT}/goal G-1 revision=1 fields=statement,rank,owner,status charter=1")]
    # its alias names it; a goal by hand goes first, and a later charter's new goal comes after the others
    by_alias = client.get(f"{goals_path}/night-shift", headers=headers["reader"]).json()
    assert (by_alias["code"], by_alias["revision"]) == ("G-1", 1)
    first = {"statement": "Answer every decision from a phone", "rank": 1, "deadline": "2026-11-30"}
    response = client.put(f"{goals_path}/G-2", json=first, headers=headers["owner"])
    assert response.status_code == 200, response.text
    charter_goals = [
        {"id": "night-shift", "what": "Run every approved plan at night."},  # a goal already: the charter's text stays
        {"id": "brief", "what": "A brief each morning."},
    ]
    written(client, headers["owner"], charter_body(goals=charter_goals, night_plans=[], max_decisions_per_day=1))
    paused = {"statement": "Keep the old dashboard", "status": "paused", "if_revision": 0}
    taken = client.put(f"{goals_path}/G-3", json=paused, headers=headers["owner"])  # a write makes none of the charter
    assert taken.status_code == 200 and taken.json()["revision"] == 1, taken.text
    listed = client.get(goals_path, headers=headers["reader"]).json()
    assert [(g["code"], g["alias"], g["rank"], g["status"]) for g in listed] == [
        ("G-1", "night-shift", 1, "active"),
        ("G-2", None, 1, "active"),
        ("G-3", None, 2, "paused"),
        ("G-4", "brief", 3, "active"),
    ]
    assert listed[3]["note"] == "made from goal brief of the Curator's charter, revision 2"
    assert listed[0]["statement"] == "Run the plans the owner approved while the owner sleeps."
    # the review run's prompt lists the active goals by rank, from the goals
    assert collected(client, NIGHT)["review"] == 1
    prompt = claim(client, review.worker)["prompt"]
    assert (
        "# Goals of the project, highest first\n"
        "- G-1 (night-shift): Run the plans the owner approved while the owner sleeps.\n"
        "- G-2: Answer every decision from a phone; by 2026-11-30\n"
        "- G-4 (brief): A brief each morning.\n\n"
    ) in prompt
    assert "Keep the old dashboard" not in prompt


@needs_pg
def test_goals_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
    from evo_agents.hub.server.app import create_app

    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        import httpx

        with httpx.Client(base_url=url, timeout=10) as client:
            headers = _registered(client, github)
            _signal_keys(client, headers["owner"])
        homes = {
            login: write_credentials(tmp_path / login, url, login, headers[login]["Authorization"].split()[1])
            for login, _, _ in GRANTS
        }

        def goal(who: str, *args: str) -> SimpleNamespace:
            return run_cli(monkeypatch, capsys, homes[who], "hub", "goal", *args, "--project", PROJECT)

        assert ok(goal("reader", "list")).out == f"Project {PROJECT} has no goals.\n"
        refused = goal("alice", "set", "--statement", "Fewer searches find nothing")
        assert refused.code == 1 and "needs the admin role on it; alice holds writer" in refused.err
        metric = ("--metric", "search.zero_rate", "--aggregate", "p50", "--comparator", "<=", "--target", "0.05")
        made = goal("owner", "set", "--statement", "Fewer searches find nothing", *metric, "--deadline", "2026-12-31")
        assert ok(made).out == f"Made goal G-1 of project {PROJECT}: revision 1.\n"
        changed = goal("owner", "set", "G-1", "--target", "0.03", "--window-days", "30", "--note", "tighter")
        assert ok(changed).out == f"Wrote goal G-1 of project {PROJECT}: revision 2.\n"
        same = ok(goal("owner", "set", "G-1", "--target", "0.03")).out
        assert same == f"Goal G-1 of project {PROJECT} is this already, at revision 2: nothing written.\n"
        second = goal("owner", "set", "--statement", "Answer every ticket in a day", "--owner", "alice", "--json")
        assert_json_keys("hub goal set", as_json(second))
        shown = ok(goal("reader", "show", "G-1")).out.splitlines()
        assert shown[:8] == [
            f"Goal G-1 of project {PROJECT}, revision 2",
            "  statement: Fewer searches find nothing",
            "  metric: search.zero_rate p50 over 30 days <= 0.03",
            "  deadline: 2026-12-31",
            "  canvas: -",
            "  problem: -",
            "  rank: 1",
            "  owner: owner",
        ]
        assert shown[8] == "  status: active" and shown[9].endswith("UTC: tighter")
        assert shown[10:12] == ["Revisions:", "REVISION  WRITTEN (UTC)     BY     CHANGED"]
        assert shown[12].startswith("2 ") and shown[12].endswith("owner  metric")
        listed = ok(goal("reader", "list")).out.splitlines()
        assert listed[0].split() == ["CODE", "STATUS", "RANK", "OWNER", "DEADLINE", "METRIC", "STATEMENT"]
        assert listed[1].startswith("G-1 ") and "search.zero_rate p50 over 30 days <= 0.03" in listed[1]
        assert listed[2].startswith("G-2 ") and " alice " in listed[2]
        assert_json_keys("hub goal list", as_json(goal("reader", "list", "--json")))
        assert_json_keys("hub goal show", as_json(goal("reader", "show", "G-1", "--revision", "1", "--json")))
        paused = ok(goal("owner", "set", "G-2", "--status", "paused", "--no-metric")).out
        assert paused == f"Wrote goal G-2 of project {PROJECT}: revision 2.\n"
        closed = ok(goal("owner", "close", "G-1", "--reason", "achieved", "--note", "under 3%")).out
        assert closed == f"Closed goal G-1 of project {PROJECT} (achieved): revision 3.\n"
        assert [line.split()[:2] for line in ok(goal("reader", "list", "--status", "closed")).out.splitlines()[1:]] == [
            ["G-1", "closed"]
        ]
        again = goal("owner", "close", "G-1", "--reason", "dropped", "--json")
        assert again.code == 1 and "is closed already (achieved)" in again.err
        assert_json_keys("hub goal close", as_json(goal("owner", "close", "G-2", "--reason", "dropped", "--json")))


# The problem a goal answers (plan problem-oriented-hub, criterion a2)

PROBLEM = {
    "statement": "Searches find nothing",
    "who": "shoppers of lumen",
    "when": "a search names a brand the shop sells under another name",
    "workaround": "they leave, or ask support",
}


def _problem(hub, ref: str, **changes) -> dict:
    response = hub.client.put(
        f"/v1/projects/{PROJECT}/problems/{ref}", json={**PROBLEM, **changes}, headers=hub.headers["alice"]
    )
    assert response.status_code == 200, response.text
    return response.json()


@needs_pg
def test_a_goal_answers_one_problem_of_its_project_that_is_not_dropped_and_one_without_runs_as_before(hub, hub_db):
    _problem(hub, "P-1")
    _problem(hub, "P-2", statement="Tickets wait a week")
    _problem(hub, "P-3", statement="Old search", status="dropped", close_reason="the old search is gone")
    # a goal without a problem reads and writes as before: its problem is null
    plain = _written(_put(hub, "owner", "G-1", {"statement": "Fewer searches find nothing"}))
    assert plain["problem"] is None and _get(hub, "reader", "/G-1")["problem"] is None
    for ref, said in (
        ("P-3", "problem: problem P-3 is dropped; a goal answers a problem that is open or solved"),
        ("P-9", f"problem: project {PROJECT} has no problem P-9: see `evo-agents hub problem list`"),
    ):
        refused = _put(hub, "owner", "G-1", {"statement": "Fewer searches find nothing", "problem": ref})
        assert refused.status_code == 422 and said in refused.json()["message"], refused.text
    assert _put(hub, "owner", "G-1", {"statement": "x", "problem": "G-1"}).status_code == 422  # a problem's code
    assert _put(hub, "owner", "G-1", {"statement": "x", "problem": ["P-1", "P-2"]}).status_code == 422  # one, at most
    # it names one, changes it, and clears it; each a revision whose changed fields name problem
    named = _written(_put(hub, "owner", "G-1", {"statement": "Fewer searches find nothing", "problem": "P-1"}))
    assert (named["revision"], named["problem"]) == (2, "P-1")
    moved = _written(_put(hub, "owner", "G-1", {"statement": "Fewer searches find nothing", "problem": "P-2"}))
    assert moved["problem"] == "P-2"
    assert [(item["revision"], item["changed"]) for item in _get(hub, "reader", "/G-1/revisions")][:2] == [
        (3, ["problem"]),
        (2, ["problem"]),
    ]
    # a problem dropped since the goal named it may stay named: the goal is written and closed as before
    closed = hub.client.post(
        f"/v1/projects/{PROJECT}/problems/P-2/close",
        json={"status": "dropped", "reason": "tickets go to lumen's own desk"},
        headers=hub.headers["alice"],
    )
    assert closed.status_code == 200, closed.text
    kept = _written(_put(hub, "owner", "G-1", {"statement": "Fewer empty searches", "problem": "P-2"}))
    assert (kept["revision"], kept["problem"]) == (4, "P-2")
    done = _written(_close(hub, "owner", "G-1", {"reason": "achieved"}))
    assert (done["status"], done["problem"]) == ("closed", "P-2")
    assert _put(hub, "owner", "G-2", {"statement": "y", "problem": "P-2"}).status_code == 422  # a new one may not
    assert audit_rows(hub_db, "goal")[1][1] == f"{PROJECT}/goal G-1 revision=2 fields=problem"


@needs_pg
def test_goal_set_problem_and_no_problem_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
    from evo_agents.hub.server.app import create_app

    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        import httpx

        with httpx.Client(base_url=url, timeout=10) as client:
            headers = _registered(client, github)
            made = client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=PROBLEM, headers=headers["alice"])
            assert made.status_code == 200, made.text
        homes = {
            login: write_credentials(tmp_path / login, url, login, headers[login]["Authorization"].split()[1])
            for login, _, _ in GRANTS
        }

        def goal(who: str, *args: str) -> SimpleNamespace:
            return run_cli(monkeypatch, capsys, homes[who], "hub", "goal", *args, "--project", PROJECT)

        made = as_json(
            ok(goal("owner", "set", "--statement", "Fewer searches find nothing", "--problem", "P-1", "--json"))
        )
        assert (made["code"], made["problem"]) == ("G-1", "P-1")
        shown = as_json(goal("reader", "show", "G-1", "--json"))
        assert shown["problem"] == "P-1"
        assert_json_keys("hub goal show", shown)
        assert "  problem: P-1" in ok(goal("reader", "show", "G-1")).out.splitlines()
        kept = as_json(ok(goal("owner", "set", "G-1", "--rank", "2", "--json")))
        assert (kept["revision"], kept["problem"]) == (2, "P-1"), "an option left out keeps the problem"
        cleared = as_json(ok(goal("owner", "set", "G-1", "--no-problem", "--json")))
        assert (cleared["revision"], cleared["problem"]) == (3, None)
        refused = goal("owner", "set", "G-1", "--problem", "P-7")
        assert refused.code == 1 and f"project {PROJECT} has no problem P-7" in refused.err
        assert_json_keys("hub goal list", as_json(goal("reader", "list", "--json")))
