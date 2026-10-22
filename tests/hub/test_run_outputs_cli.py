"""``evo-agents hub run outputs`` (plan run-outputs, criterion a10): the outputs of a run as a table and as ``--json``
with the keys the contract declares, and ``--download DIR`` saving each under its own name, checked against its
SHA-256, next to a file of that name with other bytes and never outside DIR; against a hub under uvicorn and a fake S3,
as a signed-in member runs it. The naming of the saved files runs without Postgres."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub.client import HubError
from evo_agents.hub.run_cli_outputs import place, size_text
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_output_cli_names_a_saved_file_by_its_own_name_beside_another_and_never_outside(tmp_path):
    (tmp_path / "report.md").write_bytes(b"other bytes")
    import hashlib

    mine = hashlib.sha256(b"mine").hexdigest()
    assert place(tmp_path, "report.md", mine) == (tmp_path.resolve() / "report (2).md", False)
    (tmp_path / "report (2).md").write_bytes(b"mine")
    assert place(tmp_path, "report.md", mine) == (tmp_path.resolve() / "report (2).md", True)
    assert place(tmp_path, "shots/a.png", mine) == (tmp_path.resolve() / "shots" / "a.png", False)
    for bad in ("../a.png", "/etc/a.png", "a/../../b.png"):
        with pytest.raises(HubError):
            place(tmp_path, bad, mine)
    assert [size_text(n) for n in (512, 1536, 200 * 1024 * 1024)] == ["512 B", "1.5 KiB", "200.0 MiB"]


if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_run_outputs import PNG, REPORT, item, sent
    from tests.hub.test_run_stream import members, serving
    from tests.hub.test_runs import OWNER, PROJECT, READER, add_worker, claim, dispatched, moved


@pytest.fixture
def hub(hub_db, tmp_path, github, s3):
    """The hub under uvicorn with a fake S3, test_run_stream's members, and homes of owner and reader signed in."""
    app = create_app(live.hub_config(hub_db, tmp_path, github, **s3.config()))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = members(client, github)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("reader", READER))
        }
        yield SimpleNamespace(url=url, client=client, headers=headers, homes=homes, db=hub_db)


def outputs_cli(hub, monkeypatch, capsys, who: str, *args: str):
    return cli(monkeypatch, capsys, hub.homes[who], "hub", "run", "outputs", *args, "--project", PROJECT)


@needs_pg
def test_output_cli_lists_a_runs_outputs_and_downloads_them_under_their_own_names(hub, monkeypatch, capsys, tmp_path):
    worker = add_worker(hub.client, hub.headers["owner"], "mac-mini")
    run_id = dispatched(hub.client, hub.headers["owner"], [2])[0]["id"]
    assert claim(hub.client, worker)["id"] == run_id
    moved(hub.client, worker, run_id, "running")
    files = [
        (PNG, item(PNG, "screens/login.png", "image/png", "tool")),
        (REPORT, item(REPORT, "report.md", "text/markdown", caption="What changed")),
    ]
    sent(hub.client, f"/v1/worker/runs/{run_id}", worker["headers"], files)

    found = as_json(outputs_cli(hub, monkeypatch, capsys, "reader", str(run_id), "--json"))
    assert_json_keys("hub run outputs", found)
    assert [o["name"] for o in found["outputs"]] == ["screens/login.png", "report.md"]
    only = as_json(outputs_cli(hub, monkeypatch, capsys, "reader", str(run_id), "--source", "sent", "--json"))
    assert [o["name"] for o in only["outputs"]] == ["report.md"]
    lines = ok(outputs_cli(hub, monkeypatch, capsys, "reader", str(run_id))).out.splitlines()
    assert lines[0].split() == ["ID", "SOURCE", "NAME", "TYPE", "SIZE", "CAPTION"]
    assert lines[2].split()[1:5] == ["sent", "report.md", "text/markdown", f"{len(REPORT)}"]
    assert lines[-1] == f"2 output(s), {len(PNG) + len(REPORT)} B, on the hub for run #{run_id}."

    target = tmp_path / "saved"
    (target).mkdir()
    (target / "report.md").write_bytes(b"a note of mine")
    saved = ok(outputs_cli(hub, monkeypatch, capsys, "reader", str(run_id), "--download", str(target))).out
    assert (target / "screens" / "login.png").read_bytes() == PNG
    assert (target / "report (2).md").read_bytes() == REPORT
    assert (target / "report.md").read_bytes() == b"a note of mine"
    assert f"saved report.md -> {(target / 'report (2).md').resolve()}" in saved
    again = ok(outputs_cli(hub, monkeypatch, capsys, "reader", str(run_id), "--download", str(target))).out
    assert "kept screens/login.png" in again and sorted(p.name for p in target.iterdir()) == [
        "report (2).md",
        "report.md",
        "screens",
    ]
    refused = outputs_cli(hub, monkeypatch, capsys, "reader", "999999")
    assert refused.code == 1 and "has no run 999999" in refused.err
    assert not list(Path(target).glob("*.part"))
