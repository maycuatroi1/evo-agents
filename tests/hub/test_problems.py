"""The problems of a project (``evo_agents.hub.problems``, ``evo_agents.hub.server.problems`` and
``evo_agents.hub.cli_problems``), as criterion a1 of plan problem-oriented-hub names it: a problem has a statement, who
is hurt, when it happens, how they cope today, a metric with no target, a rank, an owner and a status (open, solved or
dropped, the last two with a reason); its code P-<number> is unique in the project and never given again; a writer of
the project writes it whole as its next revision, every revision is kept and audited; a reader reads it, and a member
without a grant gets 404; ``evo-agents hub problem list|show|set|close``; show lists the goals that answer it. The goal
that names a problem is ``tests.hub.test_goals``'s, @builder's proposals of one ``tests.hub.test_builder``'s, and the
tools problem_list and problem_show ``tests.hub.test_mcp``'s.

The model and the command line's parsing run without Postgres; everything that needs the hub skips without
EVO_HUB_TEST_DSN."""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import problems, tables
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
METRIC = {"key": "run.failed_rate", "aggregate": "mean", "window_days": 30}
NIGHT = {
    "statement": "  Plans the owner approved wait until morning ",
    "who": "the owner of the project",
    "when": "every night a plan is approved after six",
    "workaround": "they start the plan run by hand the next morning",
}


# The model


def test_problem_codes_metrics_and_changed_fields():
    assert (problems.code(7), problems.number_of("P-7"), problems.number_of("G-7"), problems.number_of("p-7")) == (
        "P-7",
        7,
        None,
        None,
    )
    for ref in ("P-1", "P-123456789"):
        assert re.fullmatch(problems.REF, ref), ref
    for ref in ("P-0", "P-01", "p-1", "G-1", "P-1234567890", "night", ""):
        assert not re.fullmatch(problems.REF, ref), ref
    assert problems.metric_text(None) == "no metric"
    assert problems.metric_text(METRIC) == "run.failed_rate mean over 30 days"
    assert problems.metric_text({**METRIC, "window_days": None, "tenant": "lumen"}) == (
        "run.failed_rate mean over 90 days for tenant lumen"
    )
    assert problems.status_text({"status": "open", "close_reason": None}) == "open"
    assert problems.status_text({"status": "dropped", "close_reason": "covered by P-1"}) == "dropped (covered by P-1)"
    assert problems.changed({"rank": 1, "metric": None}, {"rank": 2, "status": "open"}) == ["rank", "status"]
    assert "target" not in problems.METRIC_FIELDS and "comparator" not in problems.METRIC_FIELDS  # no target


def test_problem_commands_are_in_the_contract():
    printed = commands()
    assert printed["hub problem list"]["json"] == {
        "kind": "array",
        "keys": list(printed["hub problem list"]["json"]["keys"]),
        "schema": "ProjectProblem",
    }
    assert printed["hub problem show"]["json"]["schema"] == "ProjectProblemShown"
    assert "goals" in printed["hub problem show"]["json"]["keys"]
    for verb in ("set", "close"):
        assert printed[f"hub problem {verb}"]["json"]["schema"] == "ProjectProblemWritten"
    options = {option["flags"][0]: option for option in printed["hub problem set"]["options"]}
    assert {"--statement", "--who", "--when", "--workaround", "--metric", "--reopen"} <= set(options)
    assert "--target" not in options and "--comparator" not in options  # a problem's metric has no target
    assert options["--aggregate"]["choices"] == list(problems.AGGREGATES)
    assert [p["required"] for p in printed["hub problem set"]["positionals"]] == [False]
    close = {option["flags"][0]: option for option in printed["hub problem close"]["options"]}
    assert close["--status"]["required"] and close["--status"]["choices"] == ["solved", "dropped"]
    assert close["--reason"]["required"]
    listed = {option["flags"][0]: option for option in printed["hub problem list"]["options"]}
    assert listed["--status"]["choices"] == list(problems.STATUSES)


def test_problem_set_names_what_a_new_problem_lacks_before_asking_the_hub(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "owner", "evh_unused")

    def problem(*args: str) -> SimpleNamespace:
        return run_cli(monkeypatch, capsys, home, "hub", "problem", "set", *args, "--project", "northwind")

    result = problem("--statement", "Searches find nothing", "--who", "shoppers")
    assert result.code == 2 and result.err == "error: a new problem needs --when, --workaround\n"
    result = problem("--statement", "x", "--who", "y", "--when", "z", "--workaround", "w", "--metric", "search.zero")
    assert result.code == 2 and result.err == "error: a metric needs --aggregate\n"
    result = problem("P-1", "--no-metric", "--aggregate", "p50")
    assert result.code == 2 and "--no-metric goes without the options of a metric" in result.err
    result = problem("--reopen")
    assert result.code == 2 and "--reopen opens a problem the project has" in result.err


# On the hub

if pg.DSN:
    from sqlalchemy import func, select

    from evo_agents.hub.server import problems as problem_routes
    from evo_agents.hub.server.projects import ProjectAccess
    from evo_agents.hub.server.security import Principal, RunScope
    from tests.hub.test_autonomy_policy import GRANTS, _registered, hub  # noqa: F401 (hub is a fixture)
    from tests.hub.test_plans import PROJECT
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import audit_rows


def _signal_keys(client, headers) -> None:
    """The registry of the project: a key of kind number, one of kind feedback, and one archived later."""
    for name, kind in (("run.failed_rate", "number"), ("support.tickets", "feedback"), ("run.old_rate", "number")):
        made = client.post(f"/v1/projects/{PROJECT}/signal-keys", json={"name": name, "kind": kind}, headers=headers)
        assert made.status_code == 201, made.text


def _put(hub, who: str, ref: str, body: dict):
    return hub.client.put(f"/v1/projects/{PROJECT}/problems/{ref}", json=body, headers=hub.headers[who])


def _close(hub, who: str, ref: str, body: dict):
    return hub.client.post(f"/v1/projects/{PROJECT}/problems/{ref}/close", json=body, headers=hub.headers[who])


def _get(hub, who: str, rest: str = ""):
    response = hub.client.get(f"/v1/projects/{PROJECT}/problems{rest}", headers=hub.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def _written(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


@needs_pg
def test_problems_are_written_by_a_writer_as_revisions_kept_audited_and_read_by_members_alone(hub, hub_db, github):
    _signal_keys(hub.client, hub.headers["owner"])
    assert _get(hub, "reader") == []
    # a reader writes nothing; the hub admin without a grant reads nothing; a member without one gets 404
    first_body = {**NIGHT, "metric": METRIC, "note": " nights  first "}
    refused = _put(hub, "reader", "P-1", first_body)
    assert refused.status_code == 403
    assert refused.json()["message"] == (
        f"writing a problem of project {PROJECT} needs the writer role on it; reader holds reader"
    )
    admin = hub.client.get(f"/v1/projects/{PROJECT}/problems", headers=hub.headers["admin"])
    assert admin.status_code == 403 and "needs a grant on it" in admin.json()["message"]
    stranger = live.bearer(live.sign_in(hub.client, github, "mallory", 799)["token"])
    for path in ("", "/P-1", "/P-1/revisions"):
        outside = hub.client.get(f"/v1/projects/{PROJECT}/problems{path}", headers=stranger)
        assert outside.status_code == 404, (path, outside.text)
    assert hub.client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=NIGHT, headers=stranger).status_code == 404
    # a writer makes the first problem, ranked first and owned by its writer; the same problem again writes nothing
    first = _written(_put(hub, "alice", "P-1", first_body))
    assert (first["code"], first["number"], first["revision"], first["changed"], first["rank"]) == (
        "P-1",
        1,
        1,
        True,
        1,
    )
    assert (first["statement"], first["owner"], first["status"], first["created_by"], first["note"]) == (
        "Plans the owner approved wait until morning",
        "alice",
        "open",
        "alice",
        "nights first",
    )
    assert first["metric"] == {**METRIC, "tenant": None} and first["close_reason"] is None
    again = _written(_put(hub, "alice", "P-1", first_body))
    assert (again["revision"], again["changed"]) == (1, False)
    # a new problem takes the next code
    skipped = _put(hub, "owner", "P-3", NIGHT)
    assert skipped.status_code == 422
    assert skipped.json()["message"] == f"the next problem of project {PROJECT} is P-2, not P-3; nothing changed"
    assert _put(hub, "owner", "night", NIGHT).status_code == 422  # a code, never a name
    tickets = {"statement": "Tickets wait a week", "who": "lumen's support", "when": "on Mondays", "workaround": "-"}
    second = _written(_put(hub, "owner", "P-2", {**tickets, "owner": "alice"}))
    assert (second["rank"], second["owner"], second["metric"], second["created_by"]) == (2, "alice", None, "owner")
    # the next revision, while the problem is at revision 1 alone
    later = {**first_body, "metric": None, "rank": 3, "who": "every member who approves plans"}
    stale = _put(hub, "alice", "P-1", {**later, "if_revision": 0})
    assert stale.status_code == 409
    assert stale.json()["message"] == f"problem P-1 of project {PROJECT} is at revision 1, not 0; nothing changed"
    moved = _written(_put(hub, "alice", "P-1", {**later, "if_revision": 1}))
    assert (moved["revision"], moved["rank"], moved["metric"], moved["owner"]) == (2, 3, None, "alice")
    # every revision kept and read back, by any member
    revisions = _get(hub, "reader", "/P-1/revisions")
    assert [(item["revision"], item["status"], item["changed"]) for item in revisions] == [
        (2, "open", ["who", "metric", "rank"]),
        (1, "open", ["statement", "who", "when", "workaround", "metric", "rank", "owner", "status"]),
    ]
    assert _get(hub, "reader", "/P-1?revision=1")["metric"] == {**METRIC, "tenant": None}
    missing = hub.client.get(f"/v1/projects/{PROJECT}/problems/P-1?revision=9", headers=hub.headers["reader"])
    assert missing.status_code == 404
    assert missing.json()["message"] == f"project {PROJECT} has no problem P-1 revision 9"
    assert [p["code"] for p in _get(hub, "reader")] == ["P-2", "P-1"]  # by rank
    # closing is a revision too, once, with a reason; a solved problem opens again with a write
    solved = _written(_close(hub, "alice", "P-2", {"status": "solved", "reason": " answered  in a day "}))
    assert (solved["revision"], solved["status"], solved["close_reason"]) == (2, "solved", "answered in a day")
    twice = _close(hub, "owner", "P-2", {"status": "dropped", "reason": "x"})
    assert twice.status_code == 409
    assert twice.json()["message"] == (
        f"problem P-2 of project {PROJECT} is solved already (answered in a day); nothing changed"
    )
    assert _close(hub, "reader", "P-1", {"status": "dropped", "reason": "x"}).status_code == 403
    assert [p["code"] for p in _get(hub, "reader", "?status=solved")] == ["P-2"]
    reopened = _written(_put(hub, "alice", "P-2", {**tickets, "status": "open"}))
    assert (reopened["revision"], reopened["status"], reopened["close_reason"]) == (3, "open", None)
    dropped = _written(_close(hub, "owner", "P-1", {"status": "dropped", "reason": "the night shift runs it"}))
    assert (dropped["status"], dropped["close_reason"], dropped["created_by"]) == (
        "dropped",
        "the night shift runs it",
        "owner",
    )
    # each revision audited, by its fields and never a value
    assert audit_rows(hub_db, "problem") == [
        (
            "problem.set",
            f"{PROJECT}/problem P-1 revision=1 fields=statement,who,when,workaround,metric,rank,owner,status",
            "alice",
            PROJECT,
        ),
        (
            "problem.set",
            f"{PROJECT}/problem P-2 revision=1 fields=statement,who,when,workaround,rank,owner,status",
            "owner",
            PROJECT,
        ),
        ("problem.set", f"{PROJECT}/problem P-1 revision=2 fields=who,metric,rank", "alice", PROJECT),
        ("problem.set", f"{PROJECT}/problem P-2 revision=2 fields=status,close_reason", "alice", PROJECT),
        ("problem.set", f"{PROJECT}/problem P-2 revision=3 fields=status,close_reason", "alice", PROJECT),
        ("problem.set", f"{PROJECT}/problem P-1 revision=3 fields=status,close_reason", "owner", PROJECT),
    ]
    r = tables.problem_revisions
    assert live.sql(hub_db, select(func.count()).select_from(r))[0][0] == 6
    assert "lumen's support" not in str(audit_rows(hub_db, "problem"))


@needs_pg
def test_problem_writes_name_each_thing_wrong_with_their_metric_owner_and_shape(hub, hub_db):
    _signal_keys(hub.client, hub.headers["owner"])
    wrong = {**NIGHT, "metric": {**METRIC, "key": "run.ghost_rate"}, "owner": "reader"}
    refused = _put(hub, "alice", "P-1", wrong)
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"] == (
        f"owner: reader holds reader on project {PROJECT}; a problem's owner holds the writer role or above; "
        f"metric.key: the registry of project {PROJECT} has no key run.ghost_rate: an admin adds it with "
        "`evo-agents hub signal keys add`; nothing changed"
    )
    feedback = _put(hub, "alice", "P-1", {**NIGHT, "metric": {**METRIC, "key": "support.tickets"}})
    assert feedback.status_code == 422
    assert (
        "support.tickets is a key of kind feedback; a problem's metric is a key of kind number"
        in (feedback.json()["message"])
    )
    for shape in (
        {**NIGHT, "who": "   "},  # each of the four says something
        {key: value for key, value in NIGHT.items() if key != "workaround"},
        {**NIGHT, "status": "solved"},  # a closed problem names its reason
        {**NIGHT, "close_reason": "done"},  # and only a closed one does
        {**NIGHT, "status": "closed", "close_reason": "done"},
        {**NIGHT, "metric": {**METRIC, "target": 0.1}},  # a problem's metric has no target
        {**NIGHT, "metric": {**METRIC, "comparator": "<="}},
        {**NIGHT, "metric": {**METRIC, "window_days": 91}},
        {**NIGHT, "statement": "x" * 2001},
        {**NIGHT, "budget_usd": 50},
    ):
        assert _put(hub, "alice", "P-1", shape).status_code == 422, shape
    assert _close(hub, "alice", "P-1", {"status": "open", "reason": "x"}).status_code == 422
    assert _close(hub, "alice", "P-1", {"status": "solved", "reason": "x"}).status_code == 404
    assert _get(hub, "reader") == []
    # a key archived since a revision named it may stay; a new problem cannot name it
    kept = _written(_put(hub, "alice", "P-1", {**NIGHT, "metric": {**METRIC, "key": "run.old_rate"}}))
    archived = hub.client.post(f"/v1/projects/{PROJECT}/signal-keys/run.old_rate/archive", headers=hub.headers["owner"])
    assert archived.status_code == 200, archived.text
    assert _written(_put(hub, "alice", "P-1", {**NIGHT, "rank": 2, "metric": kept["metric"]}))["revision"] == 2
    response = _put(hub, "alice", "P-2", {**NIGHT, "metric": kept["metric"]})
    assert response.status_code == 422 and "metric.key: run.old_rate is archived" in response.json()["message"]


@needs_pg
def test_the_agent_of_a_run_never_writes_a_problem():
    access = ProjectAccess(1, None, "writer", "internal")
    agent = Principal(1, "alice", False, 1, "worker", "h", RunScope(7, PROJECT, "writer", "internal"))
    assert problem_routes.refusal(agent, access) == (
        "the agent of a run never writes a problem: @builder proposes one, which a writer approves"
    )
    assert problem_routes.refusal(Principal(1, "alice", False, 1, "machine", "h"), access) is None


@needs_pg
def test_problem_show_lists_the_goals_that_answer_it_now(hub, hub_db):
    _written(_put(hub, "alice", "P-1", NIGHT))
    goals_path = f"/v1/projects/{PROJECT}/goals"
    for ref, statement, problem in (("G-1", "Run every approved plan by night", "P-1"), ("G-2", "A brief", None)):
        body = {"statement": statement, **({"problem": problem} if problem else {})}
        made = hub.client.put(f"{goals_path}/{ref}", json=body, headers=hub.headers["owner"])
        assert made.status_code == 200, made.text
    shown = _get(hub, "reader", "/P-1")
    assert shown["goals"] == [
        {"code": "G-1", "revision": 1, "statement": "Run every approved plan by night", "status": "active"}
    ]
    assert_json_keys("hub problem show", shown)
    # a goal that stops naming it leaves the list; one that names it later joins it
    cleared = hub.client.put(f"{goals_path}/G-1", json={"statement": "Run them all"}, headers=hub.headers["owner"])
    assert cleared.status_code == 200 and cleared.json()["problem"] is None, cleared.text
    named = hub.client.put(
        f"{goals_path}/G-2", json={"statement": "A brief", "problem": "P-1"}, headers=hub.headers["owner"]
    )
    assert named.status_code == 200, named.text
    assert [goal["code"] for goal in _get(hub, "reader", "/P-1")["goals"]] == ["G-2"]
    assert _get(hub, "reader", "/P-1?revision=1")["goals"][0]["code"] == "G-2"  # the goals as they are now


@needs_pg
def test_problems_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
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

        def problem(who: str, *args: str) -> SimpleNamespace:
            return run_cli(monkeypatch, capsys, homes[who], "hub", "problem", *args, "--project", PROJECT)

        assert ok(problem("reader", "list")).out == f"Project {PROJECT} has no problems.\n"
        texts = ("--who", NIGHT["who"], "--when", NIGHT["when"], "--workaround", NIGHT["workaround"])
        refused = problem("reader", "set", "--statement", "Plans wait", *texts)
        assert refused.code == 1 and "needs the writer role on it; reader holds reader" in refused.err
        made = problem("alice", "set", "--statement", "Plans wait", *texts, "--metric", "run.failed_rate")
        assert made.code == 2 and "a metric needs --aggregate" in made.err
        made = problem(
            "alice", "set", "--statement", "Plans wait", *texts, "--metric", "run.failed_rate", "--aggregate", "mean"
        )
        assert ok(made).out == f"Made problem P-1 of project {PROJECT}: revision 1.\n"
        changed = problem("alice", "set", "P-1", "--window-days", "30", "--note", "a month")
        assert ok(changed).out == f"Wrote problem P-1 of project {PROJECT}: revision 2.\n"
        same = ok(problem("alice", "set", "P-1", "--window-days", "30")).out
        assert same == f"Problem P-1 of project {PROJECT} is this already, at revision 2: nothing written.\n"
        second = problem("owner", "set", "--statement", "Tickets wait", *texts, "--owner", "alice", "--json")
        assert_json_keys("hub problem set", as_json(second))
        shown = ok(problem("reader", "show", "P-1")).out.splitlines()
        assert shown[:9] == [
            f"Problem P-1 of project {PROJECT}, revision 2",
            "  statement: Plans wait",
            f"  who: {NIGHT['who']}",
            f"  when: {NIGHT['when']}",
            f"  workaround: {NIGHT['workaround']}",
            "  metric: run.failed_rate mean over 30 days",
            "  rank: 1",
            "  owner: alice",
            "  status: open",
        ]
        assert shown[9].endswith("UTC: a month") and shown[10] == "Goals: none answers it yet"
        assert shown[11:13] == ["Claims: none links it yet", "Plans: none names it yet"]
        assert shown[13:15] == ["Revisions:", "REVISION  WRITTEN (UTC)     BY     CHANGED"]
        assert shown[15].startswith("2 ") and shown[15].endswith("alice  metric")
        listed = ok(problem("reader", "list")).out.splitlines()
        assert listed[0].split() == ["CODE", "STATUS", "RANK", "OWNER", "METRIC", "STATEMENT"]
        assert listed[1].startswith("P-1 ") and "run.failed_rate mean over 30 days" in listed[1]
        assert listed[2].startswith("P-2 ") and " alice " in listed[2]
        as_array = as_json(problem("reader", "list", "--json"))
        assert isinstance(as_array, list) and [item["code"] for item in as_array] == ["P-1", "P-2"]
        assert_json_keys("hub problem list", as_array)
        assert_json_keys("hub problem show", as_json(problem("reader", "show", "P-1", "--revision", "1", "--json")))
        closed = ok(problem("alice", "close", "P-1", "--status", "solved", "--reason", "runs by night")).out
        assert closed == f"Closed problem P-1 of project {PROJECT} (solved): revision 3.\n"
        assert [
            line.split()[:2] for line in ok(problem("reader", "list", "--status", "solved")).out.splitlines()[1:]
        ] == [["P-1", "solved"]]
        again = problem("alice", "close", "P-1", "--status", "dropped", "--reason", "x", "--json")
        assert again.code == 1 and "is solved already (runs by night)" in again.err
        reopened = ok(problem("alice", "set", "P-1", "--reopen", "--json"))
        assert (as_json(reopened)["status"], as_json(reopened)["close_reason"]) == ("open", None)
        assert_json_keys(
            "hub problem close",
            as_json(problem("owner", "close", "P-2", "--status", "dropped", "--reason", "merged into P-1", "--json")),
        )
        assert ok(problem("reader", "show", "P-2")).out.splitlines()[8] == "  status: dropped (merged into P-1)"
