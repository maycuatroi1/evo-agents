"""``evo-agents hub srs list|show|import|export|submit|approve|reviews|withdraw|retire|archive|unarchive|findings|waive``
(``evo_agents.hub.cli_srs``), as members run them against a hub that serves the core plugin srs: the commands and their
``--json`` keys are in the contract (seam hub-cli-v1), an import prints its preview and the problems that refuse it, an
export writes the Markdown imported, approving follows the hub's rules (criterion a2 of plan srs-tasks-plugins), a
version in review is withdrawn by its author or an admin (criterion a2 of plan srs-document-lifecycle), an admin
retires items by their codes, after a dry run that writes nothing (criterion a3 of that plan), and archives a document
out of ``list`` (``list --all`` shows it) and unarchives it (criterion a1 of that plan). A member lists the findings of a
document, an approval an open finding of level decide holds prints the finding, and an admin waives it (criteria a4
and a5 of plan srs-conflicts-and-tasks); the findings are written in the table, as @ba's tool would.

The parsing runs without Postgres or the hub-server extra, as the client does; everything that needs the hub skips
without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

SAMPLE = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
PROJECT = "evo-agents"
needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_the_srs_commands_are_in_the_contract():
    printed = commands()
    verbs = (
        "list",
        "show",
        "import",
        "export",
        "submit",
        "approve",
        "reviews",
        "withdraw",
        "retire",
        "archive",
        "unarchive",
    )
    assert {f"hub srs {verb}" for verb in verbs} <= set(printed)
    listed = printed["hub srs list"]["json"]
    assert (listed["kind"], listed["schema"]) == ("array", "SrsDocumentSummary")
    assert [o["flags"][0] for o in printed["hub srs list"]["options"]] == ["--project", "--all", "--json"]
    for verb in ("archive", "unarchive"):
        command = printed[f"hub srs {verb}"]
        assert [p["metavar"] for p in command["positionals"]] == ["SLUG"]
        assert [o["flags"][0] for o in command["options"]] == ["--project", "--json"]
        assert (command["json"]["kind"], command["json"]["schema"]) == ("object", "SrsDocumentSummary")
    assert printed["hub srs import"]["json"]["schema"] == "SrsImportResult"
    assert printed["hub srs show"]["json"]["with"]["--item"]["schema"] == "SrsItemDetail"
    options = {o["flags"][0]: o for o in printed["hub srs approve"]["options"]}
    assert options["--role"]["choices"] == ["ba", "qa", "dev"] and options["--project"]["required"]
    assert options["--item"]["repeatable"]
    assert printed["hub srs approve"]["json"]["with"]["--item"]["schema"] == "SrsVersionCases"
    assert printed["hub srs reviews"]["json"]["schema"] == "SrsVersionCases"
    assert printed["hub srs export"]["json"] is None
    withdraw = printed["hub srs withdraw"]
    assert [p["metavar"] for p in withdraw["positionals"]] == ["SLUG", "N"]
    assert [o["flags"][0] for o in withdraw["options"]] == ["--project", "--comment", "--json"]
    assert withdraw["json"]["schema"] == "SrsVersion"
    retire = printed["hub srs retire"]
    assert [(p["metavar"], p["repeatable"]) for p in retire["positionals"]] == [("SLUG", False), ("CODE", True)]
    assert [o["flags"][0] for o in retire["options"]] == ["--project", "--summary", "--dry-run", "--json"]
    assert (retire["json"]["schema"], retire["json"]["keys"]) == (
        "SrsRetireResult",
        ["document", "version", "dry_run", "dropped"],
    )
    findings = printed["hub srs findings"]
    assert [p["metavar"] for p in findings["positionals"]] == ["SLUG"]
    assert [o["flags"][0] for o in findings["options"]] == ["--project", "--status", "--json"]
    assert {o["flags"][0]: o for o in findings["options"]}["--status"]["choices"] == ["open", "resolved", "waived"]
    assert (findings["json"]["kind"], findings["json"]["schema"]) == ("array", "SrsFinding")
    waive = printed["hub srs waive"]
    assert [p["metavar"] for p in waive["positionals"]] == ["SLUG", "FINDING"]
    assert [o["flags"][0] for o in waive["options"]] == ["--project", "--reason", "--json"]
    assert {o["flags"][0]: o for o in waive["options"]}["--reason"]["required"]
    assert (waive["json"]["kind"], waive["json"]["schema"]) == ("object", "SrsFinding")


def test_srs_import_takes_no_slug_or_title_with_doc(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    args = ("hub", "srs", "import", str(SAMPLE), "--project", PROJECT, "--doc", "a", "--slug", "b")
    result = run_cli(monkeypatch, capsys, home, *args)
    assert result.code == 2 and "--doc imports the next version" in result.err


if pg.DSN:  # the hub-server extra is installed (tests/hub/conftest.py)
    import httpx
    from sqlalchemy import func, insert, select

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.srs import markdown
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import serving


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn, its project with plugin srs enabled, alice and bao writers, owner its admin, and a home
    signed in for each."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        admin = live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])
        assert client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=admin).status_code == 200
        homes = {}
        for login, github_id, role in (("owner", 601, "admin"), ("alice", 602, "writer"), ("bao", 603, "writer")):
            token = live.sign_in(client, github, login, github_id)["token"]
            grant = {"role": role, "max_level": "internal"}
            assert (
                client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin).status_code == 200
            )
            homes[login] = write_credentials(tmp_path / login, url, login, token)
            if login == "owner":
                enabled = client.post(
                    f"/v1/projects/{PROJECT}/plugins/srs/enable", json={"permissions": []}, headers=live.bearer(token)
                )
                assert enabled.status_code == 200, enabled.text
        yield SimpleNamespace(url=url, homes=homes)


def srs(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "srs", *args, "--project", PROJECT)


@needs_pg
def test_srs_import_export_submit_and_approve_from_the_command_line(hub, monkeypatch, capsys, tmp_path):
    preview = ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop", "--dry-run"))
    assert preview.out.startswith("Preview of demo-shop: ") and "3 use case" in preview.out
    done = as_json(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop", "--json"))
    assert_json_keys("hub srs import", done)
    assert (done["document"], done["created"], done["version"]) == ("demo-shop", True, 1)

    listed = as_json(srs(hub, monkeypatch, capsys, "bao", "list", "--json"))
    assert_json_keys("hub srs list", listed)
    assert [(d["slug"], d["latest_version"], d["latest_state"]) for d in listed] == [("demo-shop", 1, "draft")]
    shown = ok(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop"))
    assert "v1 draft by alice" in shown.out and "UC ORD-1: Place an order" in shown.out
    detail = as_json(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop", "--item", "UC ORD-1", "--json"))
    assert_json_keys("hub srs show", detail, "--item")
    assert [child["code"] for child in detail["children"]] == ["BR ORD1-1", "BR ORD1-2"]
    document = as_json(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop", "--json"))
    assert_json_keys("hub srs show", document)

    target = tmp_path / "out.md"
    ok(srs(hub, monkeypatch, capsys, "bao", "export", "demo-shop", "--no-anchors", "--output", str(target)))
    assert target.read_text(encoding="utf-8") == markdown.normalize(SAMPLE.read_text(encoding="utf-8"))
    printed = ok(srs(hub, monkeypatch, capsys, "bao", "export", "demo-shop"))
    assert "<!-- srs: SCR-1 -->" in printed.out

    again = srs(hub, monkeypatch, capsys, "bao", "import", str(target), "--doc", "demo-shop")
    assert again.code == 1 and "nothing changed since version 1" in again.err
    taken = tmp_path / "taken.md"
    taken.write_text("# 1. More\n\n### UC ORD-1: Again\n\n| Item | Content |\n|---|---|\n| Actor | Clerk |\n")
    refused = srs(hub, monkeypatch, capsys, "bao", "import", str(taken), "--slug", "more")
    assert refused.code == 1 and "error: code_taken UC ORD-1 line 3:" in refused.err

    submitted = as_json(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1", "--json"))
    assert_json_keys("hub srs submit", submitted)
    assert submitted["state"] == "in_review"
    own = srs(hub, monkeypatch, capsys, "alice", "approve", "demo-shop", "1")
    assert own.code == 1 and "another member reviews it" in own.err
    no_comment = srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--request-changes")
    assert no_comment.code == 2
    approved = as_json(srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--json"))
    assert_json_keys("hub srs approve", approved)
    assert approved["state"] == "approved"
    final = ok(srs(hub, monkeypatch, capsys, "owner", "list"))
    assert "demo-shop  srs   1       approved  1" in final.out


@needs_pg
def test_srs_approve_item_reviews_single_use_cases_in_order_and_reviews_prints_them(hub, monkeypatch, capsys):
    ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop"))
    ok(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1"))
    stopped = srs(
        hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--item", "UC ORD-1", "--item", "UC NOPE"
    )
    assert stopped.code == 1 and stopped.out == ""
    printed = ok(srs(hub, monkeypatch, capsys, "bao", "reviews", "demo-shop", "1"))
    assert "demo-shop version 1 is in review (round 1)." in printed.out
    assert "UC ORD-1  added   approved" in printed.out and "approve by bao on v1" in printed.out
    assert "UC ORD-2  added   pending" in printed.out
    asked = srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--item", "UC ORD-2", "--request-changes")
    assert asked.code == 2
    done = as_json(
        srs(
            hub,
            monkeypatch,
            capsys,
            "bao",
            "approve",
            "demo-shop",
            "1",
            "--item",
            "UC ORD-2",
            "--request-changes",
            "--comment",
            "No actor",
            "--json",
        )
    )
    assert_json_keys("hub srs approve", done, "--item")
    assert {c["code"]: c["state"] for c in done["use_cases"]}["UC ORD-2"] == "changes_requested"
    listed = as_json(srs(hub, monkeypatch, capsys, "alice", "reviews", "demo-shop", "1", "--json"))
    assert_json_keys("hub srs reviews", listed)
    assert (listed["number"], listed["state"]) == (1, "in_review")


@needs_pg
def test_srs_withdraw_takes_a_version_out_of_review_for_good_from_the_command_line(hub, monkeypatch, capsys, tmp_path):
    ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop"))
    early = srs(hub, monkeypatch, capsys, "alice", "withdraw", "demo-shop", "1")
    assert early.code == 1 and "only a version in review is withdrawn" in early.err
    ok(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1"))
    other = srs(hub, monkeypatch, capsys, "bao", "withdraw", "demo-shop", "1")
    assert other.code == 1 and "its author or an admin" in other.err
    reason = ("--comment", "The gateway is not chosen yet")
    printed = ok(srs(hub, monkeypatch, capsys, "alice", "withdraw", "demo-shop", "1", *reason))
    assert printed.out == "demo-shop version 1 is withdrawn (round 1).\n"
    shown = ok(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop"))
    assert "v1 withdrawn by alice" in shown.out
    again = srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1")
    assert again.code == 1 and "is withdrawn: only a draft is submitted" in again.err

    fewer = tmp_path / "fewer-retries.md"
    fewer.write_text(SAMPLE.read_text(encoding="utf-8").replace("retried 3 times", "retried 2 times"), encoding="utf-8")
    ok(srs(hub, monkeypatch, capsys, "alice", "import", str(fewer), "--doc", "demo-shop"))
    ok(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "2"))
    by_admin = as_json(srs(hub, monkeypatch, capsys, "owner", "withdraw", "demo-shop", "2", "--json"))
    assert_json_keys("hub srs withdraw", by_admin)
    assert (by_admin["number"], by_admin["state"], by_admin["author"]) == (2, "withdrawn", "alice")


@needs_pg
def test_srs_retire_drops_items_by_code_after_a_dry_run_from_the_command_line(hub, monkeypatch, capsys):
    ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop"))
    writer = srs(hub, monkeypatch, capsys, "alice", "retire", "demo-shop", "UC ORD-1")
    assert writer.code == 1 and f"only an admin of project {PROJECT} retires items by their codes" in writer.err
    preview = ok(srs(hub, monkeypatch, capsys, "owner", "retire", "demo-shop", "UC ORD-1", "--dry-run"))
    assert preview.out == (
        "Retiring would drop 3 items of demo-shop; nothing is written (a dry run):\n"
        "  - UC ORD-1: Place an order (use case)\n"
        "  - BR ORD1-1: Cart rules (business rule)\n"
        "  - BR ORD1-2: Stock reservation (business rule)\n"
    )
    assert "v2" not in ok(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop")).out

    done = as_json(srs(hub, monkeypatch, capsys, "owner", "retire", "demo-shop", "UC ORD-1", "BR ORD1-2", "--json"))
    assert_json_keys("hub srs retire", done)
    assert (done["document"], done["version"], done["dry_run"]) == ("demo-shop", 2, False)
    assert [item["code"] for item in done["dropped"]] == ["UC ORD-1", "BR ORD1-1", "BR ORD1-2"]
    assert set(done["dropped"][0]) == {"key", "kind", "code", "title"}
    reason = ("--summary", "Returns move to their own document")
    printed = ok(srs(hub, monkeypatch, capsys, "owner", "retire", "demo-shop", "UC RET-1", *reason))
    assert printed.out == (
        "Saved demo-shop version 3, a draft without 2 items:\n"
        "  - UC RET-1: Return a book (use case)\n"
        "  - BR RET1-1: Return window (business rule)\n"
        f"`evo-agents hub srs submit demo-shop 3 --project {PROJECT}` sends it to review.\n"
    )
    assert "v3 draft by owner" in ok(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop")).out
    unknown = srs(hub, monkeypatch, capsys, "owner", "retire", "demo-shop", "UC RET-1", "UC ORD-2", "--dry-run")
    assert (
        unknown.code == 1 and "error: code_unknown UC RET-1: 'UC RET-1' was the code of an item dropped" in unknown.err
    )
    assert "UC ORD-2" not in unknown.err


@needs_pg
def test_srs_archive_takes_a_document_out_of_list_and_unarchive_opens_it_again_from_the_command_line(
    hub, monkeypatch, capsys
):
    ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop"))
    writer = srs(hub, monkeypatch, capsys, "alice", "archive", "demo-shop")
    assert writer.code == 1 and f"only an admin of project {PROJECT} archives a document" in writer.err
    archived = as_json(srs(hub, monkeypatch, capsys, "owner", "archive", "demo-shop", "--json"))
    assert_json_keys("hub srs archive", archived)
    assert archived["slug"] == "demo-shop" and archived["archived_at"] is not None
    assert as_json(srs(hub, monkeypatch, capsys, "bao", "list", "--json")) == []
    every = as_json(srs(hub, monkeypatch, capsys, "bao", "list", "--all", "--json"))
    assert_json_keys("hub srs list", every)
    assert [(d["slug"], d["archived_at"]) for d in every] == [("demo-shop", archived["archived_at"])]
    table = ok(srs(hub, monkeypatch, capsys, "bao", "list", "--all")).out.splitlines()
    assert table[0].split() == ["SLUG", "KIND", "LATEST", "STATE", "APPROVED", "ARCHIVED"]
    assert table[1].split()[:5] == ["demo-shop", "srs", "1", "draft", "-"] and table[1].split()[5] != "-"
    assert "v1 draft by alice" in ok(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop")).out
    closed = srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1")
    assert closed.code == 1 and f"`evo-agents hub srs unarchive demo-shop --project {PROJECT}`" in closed.err
    again = srs(hub, monkeypatch, capsys, "owner", "archive", "demo-shop")
    assert again.code == 1 and "is archived already" in again.err

    opened = ok(srs(hub, monkeypatch, capsys, "owner", "unarchive", "demo-shop"))
    assert opened.out == "Unarchived demo-shop: it is in `srs list` again and takes changes.\n"
    listed = as_json(srs(hub, monkeypatch, capsys, "bao", "list", "--json"))
    assert [(d["slug"], d["archived_at"]) for d in listed] == [("demo-shop", None)]
    unarchived = srs(hub, monkeypatch, capsys, "owner", "unarchive", "demo-shop", "--json")
    assert unarchived.code == 1 and "is not archived" in unarchived.err
    ok(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1"))
    printed = ok(srs(hub, monkeypatch, capsys, "owner", "archive", "demo-shop"))
    assert printed.out.startswith("Archived demo-shop: it is out of `srs list`, search and the knowledge graph")
    reopened = as_json(srs(hub, monkeypatch, capsys, "owner", "unarchive", "demo-shop", "--json"))
    assert_json_keys("hub srs unarchive", reopened)
    assert reopened["archived_at"] is None


def written_finding(db, level: str) -> int:
    """A finding of ``level`` on the latest version of demo-shop, about UC ORD-2, as @ba's tool writes one."""
    s = tables.srs
    d, v, i, f = s.documents, s.versions, s.items, s.findings
    ((doc, label, version),) = live.sql(
        db,
        select(d.c.id, d.c.label, func.max(v.c.id)).join(v, v.c.document_id == d.c.id).group_by(d.c.id, d.c.label),
    )
    ((item,),) = live.sql(db, select(i.c.id).where(i.c.document_id == doc, i.c.code == "UC ORD-2"))
    values = {"document_id": doc, "version_id": version, "item_id": item, "kind": "contradiction", "level": level}
    values |= {"sources": [{"kind": "code", "ref": "UC ORD-1"}], "summary": "Two holds of stock", "label": label}
    ((found,),) = live.sql(db, insert(f).values(values).returning(f.c.id))
    return found


@needs_pg
def test_srs_findings_are_listed_hold_an_approval_and_an_admin_waives_one_from_the_command_line(
    hub, hub_db, monkeypatch, capsys
):
    ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop"))
    none = ok(srs(hub, monkeypatch, capsys, "bao", "findings", "demo-shop"))
    assert none.out == "demo-shop has no finding.\n"
    ok(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1"))
    advice, decide = written_finding(hub_db, "advisory"), written_finding(hub_db, "decide")
    listed = as_json(srs(hub, monkeypatch, capsys, "bao", "findings", "demo-shop", "--json"))
    assert_json_keys("hub srs findings", listed)
    assert [(f["id"], f["level"], f["status"], f["item_code"]) for f in listed] == [
        (advice, "advisory", "open", "UC ORD-2"),
        (decide, "decide", "open", "UC ORD-2"),
    ]
    printed = ok(srs(hub, monkeypatch, capsys, "bao", "findings", "demo-shop")).out
    assert printed.startswith("1 open finding(s) of level decide hold the approval of demo-shop.\n"), printed
    assert f"#{decide} decide contradiction on v1, UC ORD-2: open" in printed and "  sources: code UC ORD-1" in printed
    held = srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1")
    assert held.code == 1 and f"#{decide} (contradiction, UC ORD-2)" in held.err, held.err
    assert f"  finding {decide}, contradiction: Two holds of stock" in held.err
    writer = srs(hub, monkeypatch, capsys, "bao", "waive", "demo-shop", str(decide), "--reason", "Settled in standup")
    assert writer.code == 1 and f"only an admin of project {PROJECT} waives a finding" in writer.err
    waived = as_json(
        srs(
            hub,
            monkeypatch,
            capsys,
            "owner",
            "waive",
            "demo-shop",
            str(decide),
            "--reason",
            "Settled in standup",
            "--json",
        )
    )
    assert_json_keys("hub srs waive", waived)
    assert (waived["status"], waived["settled_by"], waived["resolution"]) == ("waived", "owner", "Settled in standup")
    again = srs(hub, monkeypatch, capsys, "owner", "waive", "demo-shop", str(decide), "--reason", "x")
    assert again.code == 1 and "only an open finding is settled" in again.err
    shown = ok(srs(hub, monkeypatch, capsys, "bao", "findings", "demo-shop", "--status", "waived")).out
    assert f"#{decide} decide contradiction" in shown and "  waived by owner " in shown and f"#{advice}" not in shown
    assert ok(srs(hub, monkeypatch, capsys, "bao", "findings", "demo-shop", "--status", "resolved")).out == (
        "demo-shop has no finding resolved.\n"
    )
    assert as_json(srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--json"))["state"] == "approved"
