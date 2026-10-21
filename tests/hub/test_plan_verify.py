"""A step's verify is one shell command line: the hub refuses a plan write whose new verify cannot be one.

Prose in a verify ("Thủ công, với chat C, ...") used to pass the hub and fail the worker's preflight with
``missing_tool``, every word of it taken for a program. The check reads the shape alone, since the hub cannot see a
worker's PATH: the line must parse as shell, and each program it runs must have a command name
(``verify_words.shell_problem``). Only verify values new in a write are checked, on steps not done
(``plans.unrunnable_verify``), so a plan stored before the check keeps taking writes that leave its verify alone.

The pure checks run without Postgres; the routes skip without EVO_HUB_TEST_DSN.
"""

import copy

import pytest

from evo_agents.hub.plans import VERIFY_RULE, unrunnable_verify, verify_refusal
from evo_agents.hub.verify_words import COMMAND_NAME, shell_problem
from tests.hub.test_plans import counts, draft, needs_pg, plan_url, put, setup_project
from tests.hub.test_plans_api import client  # noqa: F401 (a fixture)

COMMANDS = [
    "cd web && pnpm test -- accounts",
    "gh pr view 12 --json files --jq '.files[].path' | grep -q docs/x.md",
    'python -c "import x"',
    "bash -c 'cd web && pnpm test -- accounts'",
    "env FOO=1 pytest -q",
    'for f in a b; do test -f "$f"; done',
    "test -f r.md && ! grep -q sk-ant- r.md",
    'uv run pytest -k "oauth or account"',
    "curl -fsS https://pypi.org/pypi/evo-ak/json | jq -e '.releases[\"1.2.3\"]'",
    '[ "$(git rev-parse HEAD)" = x ]',
    "/usr/bin/env python3 -V && g++ --version && python3.12 -V",
    "command -v jq >/dev/null && time -p ruff check .",
    "(cd web && pnpm lint) && ./scripts/check.sh && .venv/bin/pytest -q",
    'grep -q "Đã duyệt" "reports/Báo cáo tuần.md"',
    "evo-agents hub decision show 7 --json | jq -e '.state == \"answered\"'",
    "git diff --exit-code $(git merge-base origin/main HEAD) -- evo_agents/harness/schemas/plan.schema.json",
    'test "$(python3 -c "print(len(open(\'AGENTS.md\').read()))")" -lt 2500',
    "diff <(sort a.txt) <(sort b.txt) && test $((1 + 2)) -eq 3",
    "pytest -q  # don't skip\nruff check . &&\n  ruff format --check .",
    "python3 - <<'EOF'\nimport sys\nif not sys.argv[0].startswith('---'): sys.exit(0)\nEOF",
    'case "$x" in a) exit 0;; *) exit 1;; esac',
]

PROSE = [
    ("Từ repo evo-agents: pytest tests/hub/test_plans.py; ruff check .", "`Từ`"),
    ("Thủ công, với chat C, run R1, R2 ... ghi trong evidence: link của run", "`Thủ`"),
    ("pytest -q; Lần 2 tài liệu bằng tay", "`Lần`"),
    ("Kiểm tra bằng tay rồi ghi vào evidence", "`Kiểm`"),
    ("Manual: open /chats and send a message", "`Manual:`"),
    ("1. run the tests", "`1.`"),
    ("Done.", "`Done.`"),
    ("hai, ba lần", "`hai,`"),
    ("- chạy pytest", "`-`"),
    ("pytest -q\nsự thật nằm trong report", "`sự`"),
    ("Check the run's output by hand", "does not parse as shell"),
]


@pytest.mark.parametrize("command", COMMANDS)
def test_a_shell_command_line_passes(command):
    assert shell_problem(command) is None


@pytest.mark.parametrize("command, named", PROSE, ids=[text[:20] for text, _ in PROSE])
def test_prose_is_not_a_shell_command_line(command, named):
    why = shell_problem(command)
    assert why is not None and named in why


@pytest.mark.parametrize("name", ["pytest", "g++", "python3.12", "x86_64-linux-gnu-gcc", "pre-commit", "7z", "_x"])
def test_a_command_name_is_ascii_without_a_trailing_mark(name):
    assert COMMAND_NAME.fullmatch(name)


@pytest.mark.parametrize("word", ["Từ", "tài", "evo-agents:", "hai,", "Done.", "-v", ".hidden", "a b", "{x}", ""])
def test_a_word_of_prose_is_no_command_name(word):
    assert not COMMAND_NAME.fullmatch(word)


def plan(*verify: str, done: tuple[int, ...] = ()) -> dict:
    """A plan whose step n (from 1) has the n-th verify, done when n is in ``done``."""
    body = draft("rollout", len(verify))
    for step, text in zip(body["steps"], verify, strict=True):
        step["verify"] = text
        if step["id"] in done:
            step.update(status="done", done_at="2026-10-20", evidence="northwind@abc1234")
    return body


def test_only_new_verify_on_steps_not_done_is_checked():
    prose = "Thủ công: mở trang và xem"
    old = plan("pytest -q", prose, prose, done=(3,))
    assert [(index, key) for index, key, _ in unrunnable_verify(None, old)] == [(1, "2")]  # step 3 is done

    marked = copy.deepcopy(old)
    marked["steps"][0].update(status="done", evidence="northwind@abc1234")
    marked["steps"][1]["note"] = "still to do"
    assert unrunnable_verify(old, marked) == []  # the prose of step 2 was there already

    changed = copy.deepcopy(old)
    changed["steps"][1]["verify"] = "Thủ công: mở trang khác"
    assert [key for _, key, _ in unrunnable_verify(old, changed)] == ["2"]
    changed["steps"][1]["verify"] = "pytest -q tests/test_page.py"
    assert unrunnable_verify(old, changed) == []

    grown = copy.deepcopy(old)
    grown["steps"].append({"id": 4, "what": "more", "status": "pending", "verify": "Lần nữa"})
    assert [key for _, key, _ in unrunnable_verify(old, grown)] == ["4"]

    reopened = copy.deepcopy(old)
    reopened["steps"][2].update(status="pending")
    assert unrunnable_verify(old, reopened) == []  # step 3 kept its verify; reopening it is no new verify


def test_the_refusal_names_each_step_and_word_and_says_what_to_do():
    body = plan("Thủ công: mở trang", "pytest -q", "Check the run's output")
    message = verify_refusal("rollout", unrunnable_verify(None, body))
    assert message.startswith("plan rollout: the verify of step 1 runs `Thủ` as a program")
    assert "the verify of step 3 does not parse as shell" in message
    assert "step 2" not in message
    assert VERIFY_RULE in message and message.endswith("Nothing was written")
    for words in ("one shell command line", "note", "machine-readable", "hub decision", "report file"):
        assert words in message


# The routes


@needs_pg
def test_a_new_plan_with_prose_in_a_verify_is_refused_and_nothing_is_written(client, github, hub_db):  # noqa: F811
    hubs = setup_project(client, github)
    before = counts(hub_db)
    refused = put(hubs["alice"], plan("pytest -q", "Thủ công, với chat C, ghi trong evidence: link của run"))
    assert refused.status_code == 422, refused.text
    message = refused.json()["message"]
    assert "the verify of step 2 runs `Thủ` as a program" in message and VERIFY_RULE in message
    assert counts(hub_db) == before

    done = put(hubs["alice"], plan("pytest -q", "Thủ công: mở trang", done=(2,)))
    assert done.status_code == 200, done.text  # a done step's verify runs no more


@needs_pg
def test_a_plan_stored_before_the_check_keeps_its_prose_until_its_verify_changes(  # noqa: F811
    client, github, hub_db, monkeypatch
):
    from evo_agents.hub.server import plans as plan_routes

    hubs = setup_project(client, github)
    alice = hubs["alice"]
    prose = "Thủ công: mở /chats và gửi một tin"
    old = plan("pytest -q", prose, "ruff check .")
    with monkeypatch.context() as before_the_check:
        before_the_check.setattr(plan_routes, "unrunnable_verify", lambda old, new: [])
        assert put(alice, old).status_code == 200

    patch = {"section": "steps", "step": 1, "updates": {"status": "done", "evidence": "northwind@abc1234"}}
    marked = client.patch(plan_url("rollout"), json={**patch, "if_revision": 1}, headers=alice.headers)
    assert marked.status_code == 200, marked.text
    assert marked.json()["revision"] == 2

    noted = copy.deepcopy(marked.json()["body"])
    noted["steps"][1]["note"] = "the manual check moves here"
    replaced = put(alice, noted, if_revision=2)
    assert replaced.status_code == 200, replaced.text

    before = counts(hub_db)
    reworded = copy.deepcopy(noted)
    reworded["steps"][1]["verify"] = "Thủ công: mở /chats rồi gửi hai tin"
    refused = put(alice, reworded, if_revision=3)
    assert refused.status_code == 422 and "the verify of step 2 runs `Thủ`" in refused.json()["message"]
    assert counts(hub_db) == before

    reworded["steps"][1]["verify"] = "grep -q 'Result: passed' reports/chats.md"
    assert put(alice, reworded, if_revision=3).status_code == 200
