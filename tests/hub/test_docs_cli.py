"""``evo-agents hub docs list|show|history|put`` (``evo_agents.hub.cli_docs``), as members run them against a hub that
serves the feature docs: the commands and their ``--json`` keys are in the contract (seam hub-cli-v1); ``put`` uploads
the images a Markdown file shows from beside it, once each, names a path the hub refuses under ``images/``, stops on a
missing image before sending anything, takes the title from the first heading, and saves the next version, none when
nothing changed; ``show --out`` writes the Markdown and its images back.

The parsing runs without Postgres or the hub-server extra, as the client does; everything that needs the hub skips
without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub.cli_docs import prepare
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

PROJECT = "evo-agents"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + secrets.token_bytes(64)
GIF = b"GIF89a" + secrets.token_bytes(64)
GUIDE = """# Coordination guide

Read the board.

![The board](shots/board.png "Tasks > Board")

![Again](./shots/board.png 'It says "Done"')
![Odd name](<../other/my board.gif>)
![On the web](https://example.org/x.png)

```markdown
![In code](not/there.png)
```
"""
needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_the_docs_commands_are_in_the_contract():
    printed = commands()
    assert {f"hub docs {verb}" for verb in ("list", "show", "history", "put")} <= set(printed)
    assert (printed["hub docs list"]["json"]["kind"], printed["hub docs list"]["json"]["schema"]) == (
        "array",
        "DocsDocumentSummary",
    )
    assert printed["hub docs show"]["json"]["schema"] == "DocsDocument"
    assert printed["hub docs history"]["json"]["schema"] == "DocsVersion"
    put = printed["hub docs put"]
    assert [p["metavar"] for p in put["positionals"]] == ["FILE"]
    flags = ["--project", "--slug", "--title", "--summary", "--base", "--label", "--json"]
    assert [o["flags"][0] for o in put["options"]] == flags
    assert (put["json"]["schema"], put["json"]["keys"]) == ("DocsSaved", ["document", "version", "created", "changed"])


def files(root: Path) -> Path:
    (root / "doc" / "shots").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "doc" / "shots" / "board.png").write_bytes(PNG)
    (root / "other" / "my board.gif").write_bytes(GIF)
    guide = root / "doc" / "coordination-guide.md"
    guide.write_text(GUIDE, encoding="utf-8")
    return guide


def test_put_names_each_local_image_once_and_leaves_urls_and_code_alone(tmp_path):
    guide = files(tmp_path)
    prepared = prepare(guide, GUIDE)
    assert prepared.title == "Coordination guide" and prepared.missing == []
    assert prepared.files == {
        "shots/board.png": (tmp_path / "doc" / "shots" / "board.png").resolve(),
        "images/my-board.gif": (tmp_path / "other" / "my board.gif").resolve(),
    }
    assert prepared.body.startswith("Read the board.\n")  # the heading is the title
    assert '![The board](shots/board.png "Tasks > Board")' in prepared.body
    assert """![Again](shots/board.png 'It says "Done"')""" in prepared.body
    assert "![Odd name](images/my-board.gif)" in prepared.body
    assert "![On the web](https://example.org/x.png)" in prepared.body
    assert "![In code](not/there.png)" in prepared.body


def test_put_stops_on_a_missing_image_before_it_sends_anything(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    guide = tmp_path / "guide.md"
    guide.write_text("# Guide\n\n![Gone](shots/gone.png)\n", encoding="utf-8")
    result = run_cli(monkeypatch, capsys, home, "hub", "docs", "put", str(guide), "--project", PROJECT)
    assert result.code == 2 and "shows images that are not there: shots/gone.png" in result.err
    bad = run_cli(monkeypatch, capsys, home, "hub", "docs", "put", str(guide), "--project", PROJECT, "--slug", "Guide!")
    assert bad.code == 2 and "is no slug" in bad.err


if pg.DSN:  # the hub-server extra is installed (tests/hub/conftest.py)
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import serving


@pytest.fixture
def hub(hub_db, tmp_path, github, s3):
    """The hub under uvicorn with a blob store, its project with the feature docs on, alice a writer, eve a reader,
    owner its admin, and a home signed in for each."""
    app = create_app(live.hub_config(hub_db, tmp_path, github, **s3.config()))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        admin = live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])
        assert client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=admin).status_code == 200
        homes = {}
        for login, github_id, role in (("owner", 601, "admin"), ("alice", 602, "writer"), ("eve", 603, "reader")):
            token = live.sign_in(client, github, login, github_id)["token"]
            grant = {"role": role, "max_level": "internal"}
            assert (
                client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin).status_code == 200
            )
            homes[login] = write_credentials(tmp_path / login, url, login, token)
            if login == "owner":
                enabled = client.post(
                    f"/v1/projects/{PROJECT}/plugins/docs/enable", json={"permissions": []}, headers=live.bearer(token)
                )
                assert enabled.status_code == 200, enabled.text
        yield SimpleNamespace(url=url, homes=homes)


def docs(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "docs", *args, "--project", PROJECT)


@needs_pg
def test_docs_put_show_and_history_from_the_command_line(hub, monkeypatch, capsys, tmp_path):
    guide = files(tmp_path / "src")
    saved = as_json(docs(hub, monkeypatch, capsys, "alice", "put", str(guide), "--summary", "first", "--json"))
    assert_json_keys("hub docs put", saved)
    assert (saved["created"], saved["changed"], saved["version"]) == (True, True, 1)
    assert saved["document"]["slug"] == "coordination-guide" and saved["document"]["title"] == "Coordination guide"
    again = ok(docs(hub, monkeypatch, capsys, "alice", "put", str(guide)))
    assert again.out == "coordination-guide version 1: unchanged, nothing saved, with 2 image(s).\n"
    guide.write_text(GUIDE + "\nOne more line.\n", encoding="utf-8")
    second = ok(docs(hub, monkeypatch, capsys, "alice", "put", str(guide), "--base", "1"))
    assert second.out == "coordination-guide version 2: saved, with 2 image(s).\n"
    stale = docs(hub, monkeypatch, capsys, "alice", "put", str(guide), "--base", "1", "--title", "Other")
    assert stale.code == 1 and "is at version 2, not 1" in stale.err

    listed = as_json(docs(hub, monkeypatch, capsys, "eve", "list", "--json"))
    assert_json_keys("hub docs list", listed)
    assert [(d["slug"], d["latest_version"]) for d in listed] == [("coordination-guide", 2)]
    printed = ok(docs(hub, monkeypatch, capsys, "eve", "show", "coordination-guide", "--version", "1"))
    assert printed.out.startswith("# Coordination guide\n\nRead the board.\n")
    document = as_json(docs(hub, monkeypatch, capsys, "eve", "show", "coordination-guide", "--json"))
    assert_json_keys("hub docs show", document)
    assert sorted(image["name"] for image in document["shown"]["images"]) == ["images/my-board.gif", "shots/board.png"]
    history = as_json(docs(hub, monkeypatch, capsys, "eve", "history", "coordination-guide", "--json"))
    assert_json_keys("hub docs history", history)
    assert [(v["number"], v["summary"]) for v in history] == [(2, ""), (1, "first")]

    out = tmp_path / "out"
    written = ok(docs(hub, monkeypatch, capsys, "eve", "show", "coordination-guide", "--out", str(out)))
    assert written.out.startswith("Wrote version 2 of coordination-guide to ")
    assert (out / "shots" / "board.png").read_bytes() == PNG
    assert (out / "images" / "my-board.gif").read_bytes() == GIF
    assert (out / "coordination-guide.md").read_text(encoding="utf-8").startswith("# Coordination guide\n\nRead the")
    refused = docs(hub, monkeypatch, capsys, "eve", "put", str(guide))
    assert refused.code == 1 and "writer role" in refused.err
