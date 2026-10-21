"""The outputs of runs on the hub (plan run-outputs, step 1): what a worker and a local session send, what readers list
and read, the bytes served from the hub's origin, the limits, the project's switch of tool images, deleting one, the
questions that carry outputs, and the retention of tool images with the run's events.

Criteria a7, a8 and a10 (its hub part) of the plan, and the hub's part of a4: an output belongs to a run and its
readers alone read it (a local run's to its member); its bytes carry their type, nosniff and a sandbox, HTML and SVG
never with the hub's origin; the dispatcher or an admin deletes one, audited, and its bytes go once nothing refers to
them; 200 MiB a file, 500 outputs and 1 GiB a run, refused with 413 naming the limit; tool images go with the run's
events unless something carries them, the other sources stay. The people, projects and files are fictional.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.hub import blobs, outputs, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import queue
from tests.hub import live
from tests.hub.live import bearer, sql
from tests.hub.s3 import get_url, put_presigned
from tests.hub.test_runs import (
    PROJECT,
    add_worker,
    claim,
    dispatched,
    members,
    moved,
    set_run,
)

LEAD = "lead-admin"  # an admin of the project
LEAD_GRANT = {"role": "admin", "max_level": "internal"}
PNG = b"\x89PNG\r\n\x1a\n" + b"northwind screenshot" * 20
REPORT = b"# Report\n\nThe flaky wait test passes now.\n"
VIDEO = bytes(range(256)) * 4  # 1 KiB standing in for an mp4
SESSION = "0199a3c1-0000-7000-8000-00000000b001"
REGISTER = {"machine": "mac-mini", "cwd": "/Users/lumen/evo-agents", "claude_email": "lumen@example.com"}
YES_NO = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """test_runs' members (owner and someone-else writers, reader a reader, a stranger, a hub admin without a grant)
    and lead-admin, an admin of the project."""
    headers = members(client, github)
    headers["lead"] = bearer(live.sign_in(client, github, LEAD, 690)["token"])
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{LEAD}", json=LEAD_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    return headers


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def item(data: bytes, name: str, media_type: str, source: str = "sent", **extra) -> dict:
    return {"sha256": sha(data), "size": len(data), "source": source, "name": name, "media_type": media_type, **extra}


def send(client, prefix: str, headers: dict, files: list[tuple[bytes, dict]]):
    """Upload the bytes of ``files`` (data, output) that the project does not hold, then record the outputs, as a
    machine does: the answer of the record."""
    asked = client.post(
        f"{prefix}/output-uploads",
        json={"items": [{key: output[key] for key in ("sha256", "size", "source")} for _, output in files]},
        headers=headers,
    )
    if asked.status_code != 200:
        return asked
    tickets = {ticket["sha256"]: ticket for ticket in asked.json()["uploads"]}
    recorded = []
    for data, output in files:
        ticket = tickets.get(output["sha256"])
        if ticket is not None:
            assert put_presigned(ticket["url"], data) == 200
        recorded.append({**output, "upload_id": ticket["upload_id"] if ticket else None})
    return client.post(f"{prefix}/outputs", json={"outputs": recorded}, headers=headers)


def sent(client, prefix: str, headers: dict, files: list[tuple[bytes, dict]]) -> dict:
    response = send(client, prefix, headers, files)
    assert response.status_code == 200, response.text
    return response.json()


def running(client, hub, step: int = 2) -> tuple[dict, int, str]:
    """A worker of owner holding a run of ``step``: (worker, run id, the worker's prefix of the run)."""
    worker = add_worker(client, hub["owner"], f"mac-{step}")
    run_id = dispatched(client, hub["owner"], [step])[0]["id"]
    assert claim(client, worker)["id"] == run_id
    moved(client, worker, run_id, "running")
    return worker, run_id, f"/v1/worker/runs/{run_id}"


def outputs_of(client, headers, run_id: int, **params):
    return client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/outputs", params=params, headers=headers)


def listed(client, headers, run_id: int, **params) -> dict:
    response = outputs_of(client, headers, run_id, **params)
    assert response.status_code == 200, response.text
    return response.json()


def output_events(db, run_id: int) -> list[dict]:
    e = tables.run_events
    query = select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "system").order_by(e.c.seq)
    return [body for (body,) in sql(db, query) if "outputs" in body or "outputs_deleted" in body]


# Sending and reading


def test_a_worker_sends_outputs_that_readers_list_and_read_and_a_file_sent_again_is_the_same_output(
    client, hub, hub_db
):
    worker, run_id, prefix = running(client, hub)
    shot = item(PNG, "screens/login.png", "image/png", "tool", tool_call_id="toolu_01")
    report = item(REPORT, "report.md", "text/markdown", caption="What changed, for the owner")
    answer = sent(client, prefix, worker["headers"], [(PNG, shot), (REPORT, report)])
    assert answer["added"] == 2
    first, second = answer["outputs"]
    assert (first["name"], first["source"], first["viewer"], first["tool_call_id"]) == (
        "screens/login.png",
        "tool",
        "image",
        "toolu_01",
    )
    assert (second["viewer"], second["caption"], second["size"]) == ("text", report["caption"], len(REPORT))
    assert second["url"] == f"/v1/projects/{PROJECT}/runs/{run_id}/outputs/{second['id']}/content"
    assert output_events(hub_db, run_id) == [
        {"text": "2 output(s) recorded: screens/login.png, report.md", "outputs": [first["id"], second["id"]]}
    ]
    # the same image sent on purpose is the output the run holds, kept now as sent, its bytes not uploaded again
    again = client.post(
        f"{prefix}/output-uploads",
        json={"items": [{"sha256": sha(PNG), "size": len(PNG), "source": "sent"}]},
        headers=worker["headers"],
    ).json()
    assert (again["uploads"], again["present"]) == ([], [sha(PNG)])
    resent = sent(client, prefix, worker["headers"], [(PNG, item(PNG, "login.png", "image/png", caption="Logged in"))])
    assert resent["added"] == 0
    (kept,) = resent["outputs"]
    assert (kept["id"], kept["source"], kept["name"], kept["caption"]) == (
        first["id"],
        "sent",
        "screens/login.png",
        "Logged in",
    )

    shown = listed(client, hub["reader"], run_id)
    assert (shown["count"], shown["bytes"], shown["tool_images"]) == (2, len(PNG) + len(REPORT), True)
    assert [o["id"] for o in shown["outputs"]] == [first["id"], second["id"]]
    assert [o["id"] for o in listed(client, hub["reader"], run_id, source="sent")["outputs"]] == [
        first["id"],
        second["id"],
    ]
    assert listed(client, hub["reader"], run_id, after=first["id"])["outputs"][0]["id"] == second["id"]
    one = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/outputs/{second['id']}", headers=hub["reader"])
    assert one.status_code == 200 and one.json()["name"] == "report.md"
    content = client.get(second["url"], headers=hub["reader"])
    assert (content.status_code, content.content) == (200, REPORT)
    for who, status in (("stranger", 404), ("admin", 403)):
        assert outputs_of(client, hub[who], run_id).status_code == status, who
        assert client.get(second["url"], headers=hub[who]).status_code == status, who
    other = add_worker(client, hub["other"], "linux-box")
    assert send(client, prefix, other["headers"], [(REPORT, report)]).status_code == 404


def test_an_output_is_refused_for_a_name_type_or_upload_that_is_not_its_own(client, hub):
    worker, run_id, prefix = running(client, hub)
    for bad in (
        item(REPORT, "../escape.md", "text/markdown"),
        item(REPORT, "/abs.md", "text/markdown"),
        item(REPORT, "a\\b.md", "text/markdown"),
        item(REPORT, "report.md", "text/markdown; charset=utf-8"),
        item(REPORT, "report.md", "markdown"),
        item(REPORT, "report.md", "text/markdown", caption=""),
        item(REPORT, "report.md", "text/markdown", source="screen"),
    ):
        refused = client.post(f"{prefix}/outputs", json={"outputs": [bad]}, headers=worker["headers"])
        assert refused.status_code == 422, bad
    # bytes the project does not hold are uploaded first
    missing = client.post(
        f"{prefix}/outputs", json={"outputs": [item(VIDEO, "clip.mp4", "video/mp4")]}, headers=worker["headers"]
    )
    assert missing.status_code == 409 and "does not hold the bytes" in missing.json()["message"]
    # an upload names the bytes it was asked for
    ticket = client.post(
        f"{prefix}/output-uploads",
        json={"items": [{"sha256": sha(VIDEO), "size": len(VIDEO), "source": "sent"}]},
        headers=worker["headers"],
    ).json()["uploads"][0]
    assert put_presigned(ticket["url"], VIDEO) == 200
    other = {**item(REPORT, "report.md", "text/markdown"), "upload_id": ticket["upload_id"]}
    mixed = client.post(f"{prefix}/outputs", json={"outputs": [other]}, headers=worker["headers"])
    assert mixed.status_code == 422 and "not the upload of output" in mixed.json()["message"]
    assert listed(client, hub["owner"], run_id)["count"] == 0


def test_output_limits_refuse_a_file_over_200_mib_and_a_run_past_its_outputs_or_bytes_with_413(
    client, hub, monkeypatch
):
    worker, run_id, prefix = running(client, hub)
    huge = {"sha256": "e" * 64, "size": outputs.MAX_FILE_BYTES + 1, "source": "sent"}
    over = client.post(f"{prefix}/output-uploads", json={"items": [huge]}, headers=worker["headers"])
    assert over.status_code == 413 and over.json()["detail"][0]["limit"] == "file_bytes"
    assert blobs.KIND_LIMITS[outputs.KIND] == 200 * 1024 * 1024
    monkeypatch.setattr(outputs, "MAX_RUN_OUTPUTS", 2)
    sent(
        client,
        prefix,
        worker["headers"],
        [(PNG, item(PNG, "a.png", "image/png")), (REPORT, item(REPORT, "b.md", "text/markdown"))],
    )
    third = send(client, prefix, worker["headers"], [(VIDEO, item(VIDEO, "c.mp4", "video/mp4"))])
    assert third.status_code == 413
    assert (
        third.json()["detail"][0] | {"limit": "outputs_per_run", "outputs": 2, "asked": 1} == third.json()["detail"][0]
    )
    assert "holds at most 2" in third.json()["message"]
    again = send(client, prefix, worker["headers"], [(PNG, item(PNG, "a.png", "image/png"))])
    assert again.status_code == 200, "a file the run holds counts once"
    monkeypatch.setattr(outputs, "MAX_RUN_OUTPUTS", 500)
    monkeypatch.setattr(outputs, "MAX_RUN_BYTES", len(PNG) + len(REPORT) + 10)
    heavy = send(client, prefix, worker["headers"], [(VIDEO, item(VIDEO, "c.mp4", "video/mp4"))])
    assert heavy.status_code == 413 and heavy.json()["detail"][0]["limit"] == "bytes_per_run"
    assert listed(client, hub["owner"], run_id)["count"] == 2


# The project's switch


def test_the_tool_images_switch_of_an_admin_refuses_tool_outputs_and_the_machines_read_it(client, hub, hub_db):
    worker, run_id, prefix = running(client, hub)
    settings = f"/v1/projects/{PROJECT}/output-settings"
    shown = client.get(settings, headers=hub["reader"]).json()
    assert shown == {
        "project": PROJECT,
        "tool_images": True,
        "max_file_bytes": outputs.MAX_FILE_BYTES,
        "max_outputs": outputs.MAX_RUN_OUTPUTS,
        "max_run_bytes": outputs.MAX_RUN_BYTES,
    }
    for who, status in (("owner", 403), ("reader", 403), ("stranger", 404)):
        assert client.put(settings, json={"tool_images": False}, headers=hub[who]).status_code == status, who
    assert client.put(settings, json={"tool_images": False, "extra": 1}, headers=hub["lead"]).status_code == 422
    off = client.put(settings, json={"tool_images": False}, headers=hub["lead"])
    assert off.status_code == 200 and off.json()["tool_images"] is False
    machine = client.get(f"{prefix}/output-settings", headers=worker["headers"]).json()
    assert (machine["tool_images"], machine["run_id"], machine["outputs"], machine["bytes"]) == (False, run_id, 0, 0)
    tool = item(PNG, "shot.png", "image/png", "tool")
    refused = send(client, prefix, worker["headers"], [(PNG, tool)])
    assert refused.status_code == 409 and "turned them off" in refused.json()["message"]
    assert send(client, prefix, worker["headers"], [(PNG, {**tool, "source": "sent"})]).status_code == 200
    assert client.put(settings, json={"tool_images": True}, headers=hub["lead"]).json()["tool_images"] is True
    a = tables.audit
    rows = sql(hub_db, select(a.c.target).where(a.c.action == "settings.outputs").order_by(a.c.id))
    assert rows == [(f"{PROJECT}/outputs tool_images=off",), (f"{PROJECT}/outputs tool_images=on",)]


# The bytes


def test_output_bytes_carry_their_type_nosniff_and_a_sandbox_and_serve_one_range(client, hub):
    worker, run_id, prefix = running(client, hub)
    page = b"<html><script>fetch('/v1/auth/whoami')</script><p>Lumen</p></html>"
    drawing = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    archive = b"PK\x03\x04 northwind"
    files = [
        (VIDEO, item(VIDEO, "demo.mp4", "video/mp4")),
        (page, item(page, "artifact.html", "text/html", caption="Published at https://claude.ai/artifact/lumen")),
        (drawing, item(drawing, "chart.svg", "image/svg+xml")),
        (archive, item(archive, "build.zip", "application/zip")),
        (REPORT, item(REPORT, "Báo cáo tuần.md", "text/markdown")),
    ]
    video, html, svg, zipped, markdown = sent(client, prefix, worker["headers"], files)["outputs"]

    whole = client.get(video["url"], headers=hub["reader"])
    assert (whole.status_code, whole.content, whole.headers["content-type"]) == (200, VIDEO, "video/mp4")
    assert whole.headers["x-content-type-options"] == "nosniff"
    assert whole.headers["cross-origin-resource-policy"] == "same-origin"
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.headers["etag"] == f'"{sha(VIDEO)}"'
    assert whole.headers["content-security-policy"].startswith("sandbox;")
    assert whole.headers["content-disposition"].startswith('inline; filename="demo.mp4"')
    part = client.get(video["url"], headers={**hub["reader"], "Range": "bytes=2-5"})
    assert (part.status_code, part.content) == (206, VIDEO[2:6])
    assert (part.headers["content-range"], part.headers["content-length"]) == (f"bytes 2-5/{len(VIDEO)}", "4")
    tail = client.get(video["url"], headers={**hub["reader"], "Range": "bytes=-3"})
    assert (tail.status_code, tail.content) == (206, VIDEO[-3:])
    rest = client.get(video["url"], headers={**hub["reader"], "Range": "bytes=1000-"})
    assert (rest.status_code, rest.content) == (206, VIDEO[1000:])
    outside = client.get(video["url"], headers={**hub["reader"], "Range": f"bytes={len(VIDEO)}-"})
    assert outside.status_code == 416 and outside.headers["content-range"] == f"bytes */{len(VIDEO)}"
    several = client.get(video["url"], headers={**hub["reader"], "Range": "bytes=0-1,4-5"})
    assert (several.status_code, several.content) == (200, VIDEO), "several ranges: the whole file"
    cached = client.get(video["url"], headers={**hub["reader"], "If-None-Match": f'"{sha(VIDEO)}"'})
    assert (cached.status_code, cached.content) == (304, b"")

    shown = client.get(html["url"], headers=hub["reader"])
    policy = shown.headers["content-security-policy"]
    assert shown.headers["content-type"] == "text/html; charset=utf-8" and shown.content == page
    assert "sandbox allow-scripts" in policy and "allow-same-origin" not in policy
    assert "connect-src 'none'" in policy and "frame-ancestors 'self'" in policy
    image = client.get(svg["url"], headers=hub["reader"])
    assert image.headers["content-type"] == "image/svg+xml"
    assert image.headers["content-security-policy"] == outputs.OPAQUE_POLICY
    assert "script-src" not in image.headers["content-security-policy"]
    saved = client.get(zipped["url"], headers=hub["reader"])
    assert saved.headers["content-disposition"].startswith('attachment; filename="build.zip"')
    text = client.get(markdown["url"], headers=hub["reader"])
    assert text.headers["content-type"] == "text/plain; charset=utf-8" and text.content == REPORT
    assert text.headers["content-disposition"] == (
        "inline; filename=\"B_o_c_o_tu_n.md\"; filename*=UTF-8''B%C3%A1o%20c%C3%A1o%20tu%E1%BA%A7n.md"
    )
    download = client.get(markdown["url"], params={"download": "true"}, headers=hub["reader"])
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["content-type"].startswith("text/markdown")


def test_a_presigned_link_downloads_an_output_for_its_readers_alone(client, hub):
    worker, run_id, prefix = running(client, hub)
    (report,) = sent(client, prefix, worker["headers"], [(REPORT, item(REPORT, "notes/report.md", "text/markdown"))])[
        "outputs"
    ]
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/outputs/{report['id']}/link"
    link = client.get(path, headers=hub["reader"])
    assert link.status_code == 200, link.text
    found = link.json()
    assert (found["sha256"], found["size"], found["name"]) == (sha(REPORT), len(REPORT), "notes/report.md")
    assert get_url(found["url"]) == REPORT and "report.md" in found["url"]
    for who, status in (("stranger", 404), ("admin", 403)):
        assert client.get(path, headers=hub[who]).status_code == status, who
    assert (
        client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/outputs/999999/link", headers=hub["reader"]).status_code
        == 404
    )


# Deleting


def test_the_dispatcher_or_an_admin_deletes_an_output_with_audit_and_its_bytes_go_once_nothing_refers_to_them(
    client, hub, hub_db, s3
):
    worker, run_id, prefix = running(client, hub)
    shot, report = sent(
        client,
        prefix,
        worker["headers"],
        [(PNG, item(PNG, "shot.png", "image/png")), (REPORT, item(REPORT, "report.md", "text/markdown"))],
    )["outputs"]
    # another run of the project holds the same report
    other_worker, other_run, other_prefix = running(client, hub, step=4)
    sent(client, other_prefix, other_worker["headers"], [(REPORT, item(REPORT, "report.md", "text/markdown"))])
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/outputs"
    for who, status in (("reader", 403), ("other", 403), ("stranger", 404)):
        assert client.delete(f"{path}/{shot['id']}", headers=hub[who]).status_code == status, who
    gone = client.delete(f"{path}/{shot['id']}", headers=hub["owner"])
    assert gone.status_code == 200 and gone.json()["id"] == shot["id"]
    assert client.get(shot["url"], headers=hub["owner"]).status_code == 404
    assert client.delete(f"{path}/{shot['id']}", headers=hub["owner"]).status_code == 404
    assert s3.get(blobs.blob_key(sha(PNG))) is None, "no output names the image any more: its bytes went"
    b = tables.blobs
    assert sql(hub_db, select(b.c.sha256).where(b.c.sha256 == sha(PNG))) == []
    assert client.delete(f"{path}/{report['id']}", headers=hub["lead"]).status_code == 200
    assert s3.get(blobs.blob_key(sha(REPORT))) == REPORT, "the other run's output still names the report"
    left = listed(client, hub["reader"], other_run)["outputs"]
    assert client.get(left[0]["url"], headers=hub["reader"]).content == REPORT
    a = tables.audit
    rows = sql(hub_db, select(a.c.target).where(a.c.action == "run.output_delete").order_by(a.c.id))
    assert rows == [
        (f"{PROJECT}/rollout#2 run:{run_id} output:{shot['id']}",),
        (f"{PROJECT}/rollout#2 run:{run_id} output:{report['id']}",),
    ]
    assert [body.get("outputs_deleted") for body in output_events(hub_db, run_id)][-2:] == [
        [shot["id"]],
        [report["id"]],
    ]
    assert listed(client, hub["reader"], run_id)["count"] == 0


def test_output_gc_keeps_a_blob_an_output_names_whatever_else_dropped_it(client, hub, hub_db, s3):
    worker, run_id, prefix = running(client, hub)
    sent(client, prefix, worker["headers"], [(PNG, item(PNG, "shot.png", "image/png"))])
    from evo_agents.hub import blob_gc

    async def forgotten():
        async with client.app.state.engine.begin() as conn:
            await blob_gc.lock_exclusive(conn)
            b = tables.blobs
            await conn.execute(b.delete().where(b.c.sha256 == sha(PNG)))  # another part dropped its holding
            return await blob_gc.forget(conn, {sha(PNG): (len(PNG), outputs.KIND)})

    assert client.portal.call(forgotten) == [], "an output still names it: nothing to delete"
    deleted = client.portal.call(blob_gc.delete_pending, client.app.state.engine, client.app.state.blobs)
    assert deleted.objects == 0 and s3.get(blobs.blob_key(sha(PNG))) == PNG


# Questions


def test_a_question_of_a_plan_run_carries_outputs_of_its_own_run_and_its_readers_read_them(client, hub):
    from tests.hub.test_decisions import ask, asked
    from tests.hub.test_plan_runs import dispatched_plan, fleet_worker, push
    from tests.hub.test_plan_runs import plan_body as fleet_body

    push(client, hub["owner"], fleet_body())
    worker = fleet_worker(client, hub["owner"], "mac-plan")
    run_id = dispatched_plan(client, hub["owner"])["id"]
    assert claim(client, worker)["id"] == run_id
    moved(client, worker, run_id, "running")
    prefix = f"/v1/worker/runs/{run_id}"
    (shot,) = sent(client, prefix, worker["headers"], [(PNG, item(PNG, "dashboard.png", "image/png"))])["outputs"]
    step_worker, _, step_prefix = running(client, hub, step=4)
    foreign_file = [(REPORT, item(REPORT, "r.md", "text/markdown"))]
    (foreign,) = sent(client, step_prefix, step_worker["headers"], foreign_file)["outputs"]

    refused = ask(client, worker, run_id, output_ids=[foreign["id"]])
    assert refused.status_code == 422 and f"has no output {foreign['id']}" in refused.json()["message"]
    decision = asked(client, worker, run_id, output_ids=[shot["id"], shot["id"]])
    (carried,) = decision["outputs"]
    url = f"/v1/projects/{PROJECT}/decisions/{decision['id']}/outputs/{shot['id']}/content"
    assert (carried["id"], carried["url"]) == (shot["id"], url)
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision['id']}", headers=hub["reader"]).json()
    assert [o["id"] for o in shown["outputs"]] == [shot["id"]]
    page = client.get(f"/v1/projects/{PROJECT}/decisions", params={"run_id": run_id}, headers=hub["reader"]).json()
    assert [o["id"] for o in page["decisions"][0]["outputs"]] == [shot["id"]]
    assert client.get(url, headers=hub["reader"]).content == PNG
    not_carried = f"/v1/projects/{PROJECT}/decisions/{decision['id']}/outputs/{foreign['id']}/content"
    assert client.get(not_carried, headers=hub["reader"]).status_code == 404
    for who, status in (("stranger", 404), ("admin", 403)):
        assert client.get(url, headers=hub[who]).status_code == status, who


def test_a_local_session_sends_outputs_its_question_carries_and_only_its_member_reads_them(client, hub):
    registered = client.put(f"/v1/projects/{PROJECT}/local-runs/{SESSION}", json=REGISTER, headers=hub["owner"])
    assert registered.status_code == 201, registered.text
    run_id = registered.json()["id"]
    prefix = f"/v1/projects/{PROJECT}/local-runs/{run_id}"
    shot = item(PNG, "browser.png", "image/png", "tool", tool_call_id="toolu_screenshot")
    sent_file = item(REPORT, "plan.md", "text/markdown", caption="Sent with SendUserFile")
    a, b = sent(client, prefix, hub["owner"], [(PNG, shot), (REPORT, sent_file)])["outputs"]
    settings = client.get(f"{prefix}/output-settings", headers=hub["owner"]).json()
    assert (settings["outputs"], settings["bytes"]) == (2, len(PNG) + len(REPORT))
    assert send(client, prefix, hub["other"], [(REPORT, sent_file)]).status_code == 404
    body = {"question": "Ship the plan?", "options": [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]}
    asked = client.post(f"{prefix}/decisions", json={**body, "output_ids": [b["id"]]}, headers=hub["owner"])
    assert asked.status_code == 201, asked.text
    decision = asked.json()
    assert [o["name"] for o in decision["outputs"]] == ["plan.md"]
    assert client.get(decision["outputs"][0]["url"], headers=hub["owner"]).content == REPORT
    for who in ("other", "reader", "lead"):
        assert outputs_of(client, hub[who], run_id).status_code == 404, who
        assert client.get(a["url"], headers=hub[who]).status_code == 404, who
        assert client.get(decision["outputs"][0]["url"], headers=hub[who]).status_code == 404, who
    assert [o["id"] for o in listed(client, hub["owner"], run_id)["outputs"]] == [a["id"], b["id"]]


# Retention


def test_tool_outputs_prune_with_their_runs_events_and_sent_or_carried_outputs_stay(client, hub, hub_db, s3):
    worker, run_id, prefix = running(client, hub)
    _, other_shot, report = sent(
        client,
        prefix,
        worker["headers"],
        [
            (PNG, item(PNG, "shot.png", "image/png", "tool")),
            (VIDEO, item(VIDEO, "kept.png", "image/png", "tool")),
            (REPORT, item(REPORT, "report.md", "text/markdown")),
        ],
    )["outputs"]
    k = tables.run_output_links
    d = tables.decisions
    decision_id = sql(
        hub_db,
        d.insert()
        .values(run_id=run_id, project_id=project_id(hub_db), category="scope", question="Keep it?", options=YES_NO)
        .returning(d.c.id),
    )[0][0]
    sql(hub_db, k.insert().values(output_id=other_shot["id"], decision_id=decision_id))
    moved(client, worker, run_id, "failed", error="the agent gave up")
    set_run(hub_db, run_id, finished_at=func.now() - timedelta(days=31))
    report_ = prune(client)
    assert (report_["tool_outputs"], report_["tool_output_bytes"]) == (1, len(PNG))
    assert [o["id"] for o in listed(client, hub["owner"], run_id)["outputs"]] == [other_shot["id"], report["id"]]
    assert s3.get(blobs.blob_key(sha(PNG))) is None
    assert s3.get(blobs.blob_key(sha(VIDEO))) == VIDEO and s3.get(blobs.blob_key(sha(REPORT))) == REPORT
    assert prune(client)["tool_outputs"] == 0


def project_id(db) -> int:
    p = tables.projects
    return sql(db, select(p.c.id).where(p.c.name == PROJECT))[0][0]


def prune(client) -> dict:
    from evo_agents.hub import jobs

    state = client.app.state
    found = SimpleNamespace(
        engine=state.engine, config=state.config, sealer=state.sealer, github_app=state.github_app, blobs=state.blobs
    )
    return client.portal.call(
        queue.tasks[jobs.PRUNE_RUN_EVENTS].func, SimpleNamespace(additional_context={"hub": found})
    )
