"""Interactive answers in chats (plan chat-interactive-answers, step 1): an agent marks an HTML file it sends as an
interactive answer, the hub refuses the mark on any other file, serves a marked file under a policy that loads
libraries from a fixed list of CDNs only in a chat that is not restricted, read when it serves it, and a person's
question sent from the answer goes in its thread to the agent that wrote it, at most ``chats.ANSWER_PROMPTS`` of a
person in ``chats.ANSWER_WINDOW`` seconds. The people, projects and files are fictional, as in
``tests.hub.test_chat_runs``, whose world this reuses.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import chat_runs, chats, outputs
from evo_agents.hub.server.app import create_app
from evo_agents.worker.home import WorkerStateError
from evo_agents.worker.output_cli import checked_files
from tests.hub import test_credentials_api as credentials_api
from tests.hub.test_chat_runs import (
    OTHER,
    chat_runs_of,
    chat_worker,
    created,
    ended,
    hub,
    in_chats,
    started,
    thread,
)
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_run_outputs import REPORT, item, send, sent
from tests.hub.test_runs import PROJECT

app_key = credentials_api.app_key  # a fixture
__all__ = ["hub"]  # a fixture of test_chat_runs, on this module's client

CHART = b"<!doctype html><title>Wait times</title><script type=module src='https://esm.sh/chart.js'></script>\n"
# Hub sinks cleared for customer: the project, and a chat of its agents, is restricted (``telegram.restricted``).
CUSTOMER_SINKS = [{**sink, "clearance": {"level": "customer", "location": "any"}} for sink in OPEN_SINKS]


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3):  # noqa: F811
    config = replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        client.db = hub_db  # the hub of test_chat_runs gives its agents an account pool
        yield client


def answered(client, hub, worker: dict, chat: dict) -> tuple[dict, dict, dict, dict]:
    """@reviewer's run in ``chat`` sends CHART marked interactive and posts it in the thread of the owner's mention:
    (mention, spec, the output, the agent's message)."""
    mention, spec = started(client, hub, worker, chat)
    prefix = f"/v1/worker/runs/{spec['id']}"
    (chart,) = sent(
        client, prefix, worker["headers"], [(CHART, item(CHART, "waits.html", "text/html", interactive=True))]
    )["outputs"]
    posted = in_chats(
        client, worker, spec["id"], "chat/messages", {"body": "Pick a week.", "output_ids": [chart["id"]]}
    )
    assert posted.status_code == 201, posted.text
    return mention, spec, chart, posted.json()


def asked(client, headers, chat_id: int, output_id: int, text: str = "Show the week of the release", **extra):
    body = {"body": text, "from_answer": output_id, **extra}
    return client.post(f"/v1/chats/{chat_id}/messages", json=body, headers=headers)


def test_agent_marks_html_output_interactive(client, hub):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec, chart, posted = answered(client, hub, worker, chat)
    assert chart["interactive"] is True
    assert [(o["id"], o["interactive"]) for o in posted["outputs"]] == [(chart["id"], True)]
    shown = {m["id"]: m for m in thread(client, hub["other"], chat["id"], mention["id"])}
    assert [(o["id"], o["interactive"]) for o in shown[posted["id"]]["outputs"]] == [(chart["id"], True)]
    # an output sent without the mark stays as before
    (report,) = sent(
        client,
        f"/v1/worker/runs/{spec['id']}",
        worker["headers"],
        [(REPORT, item(REPORT, "report.md", "text/markdown"))],
    )["outputs"]
    assert report["interactive"] is False
    listed = client.get(f"/v1/projects/{PROJECT}/runs/{spec['id']}/outputs", headers=hub["owner"])
    assert listed.status_code == 200, listed.text
    assert {o["name"]: o["interactive"] for o in listed.json()["outputs"]} == {"waits.html": True, "report.md": False}


def test_interactive_refuses_non_html(client, hub, tmp_path):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    _, spec = started(client, hub, worker, chat)
    prefix = f"/v1/worker/runs/{spec['id']}"
    refused = send(
        client, prefix, worker["headers"], [(REPORT, item(REPORT, "report.md", "text/markdown", interactive=True))]
    )
    assert refused.status_code == 422, refused.text
    assert "an interactive answer is an HTML file" in refused.text
    big = b"<!doctype html>" + b" " * outputs.MAX_INTERACTIVE_BYTES
    too_big = send(client, prefix, worker["headers"], [(big, item(big, "big.html", "text/html", interactive=True))])
    assert too_big.status_code == 422 and f"at most {outputs.MAX_INTERACTIVE_BYTES} bytes" in too_big.text
    # the worker's command refuses them before they leave the machine, and says why
    (tmp_path / "report.md").write_bytes(REPORT)
    (tmp_path / "big.html").write_bytes(big)
    (tmp_path / "waits.html").write_bytes(CHART)
    for name, why in (("report.md", "is an HTML file (.html), not text/markdown"), ("big.html", "bytes, and this one")):
        with pytest.raises(WorkerStateError, match="cannot be an interactive answer") as refusal:
            checked_files([str(tmp_path / name)], 10, interactive=True)
        assert why in str(refusal.value)
    assert [f.name for f in checked_files([str(tmp_path / "waits.html")], 10, interactive=True)] == ["waits.html"]
    assert [f.name for f in checked_files([str(tmp_path / "report.md")], 10)] == ["report.md"], "unmarked: as before"


def test_cdn_policy_only_in_unrestricted_chat(client, hub):
    assert "esm.sh" not in outputs.HTML_POLICY and "connect-src 'none'" in outputs.HTML_POLICY
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    _, spec, chart, _ = answered(client, hub, worker, chat)
    in_chat = f"/v1/chats/{chat['id']}/outputs/{chart['id']}/content"
    of_run = f"/v1/projects/{PROJECT}/runs/{spec['id']}/outputs/{chart['id']}/content"
    served = client.get(in_chat, headers=hub["other"])
    assert (served.status_code, served.content) == (200, CHART)
    policy = served.headers["content-security-policy"]
    assert policy == outputs.INTERACTIVE_POLICY
    for host in outputs.CDN_HOSTS:
        assert host in policy
    assert "connect-src 'none'" in policy and "form-action 'none'" in policy
    assert "sandbox" in policy and "allow-same-origin" not in policy
    assert served.headers["cache-control"] == "private, no-cache"
    # outside a chat the answer is any HTML file
    assert client.get(of_run, headers=hub["owner"]).headers["content-security-policy"] == outputs.HTML_POLICY
    # the chat turns restricted: the next read gets the policy of any HTML file, from the same cached bytes too
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": CUSTOMER_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200
    assert client.get(f"/v1/chats/{chat['id']}", headers=hub["owner"]).json()["restricted"] is True
    restricted = client.get(in_chat, headers=hub["other"])
    assert (restricted.status_code, restricted.headers["content-security-policy"]) == (200, outputs.HTML_POLICY)
    again = client.get(in_chat, headers={**hub["other"], "If-None-Match": served.headers["etag"]})
    assert (again.status_code, again.headers["content-security-policy"]) == (304, outputs.HTML_POLICY)


def test_follow_up_from_answer_wakes_its_agent(client, hub, hub_db):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    mention, spec, chart, posted = answered(client, hub, worker, chat)
    ended(client, worker, spec["id"])
    # someone else wrote in the thread: a message without an @ no longer wakes the owner's session there
    other = client.post(
        f"/v1/chats/{chat['id']}/messages", json={"body": "nice", "reply_to": mention["id"]}, headers=hub["other"]
    )
    assert other.status_code == 201, other.text
    response = asked(client, hub["owner"], chat["id"], chart["id"])
    assert response.status_code == 201, response.text
    follow_up = response.json()
    assert follow_up["from_answer"] == chart["id"]
    (_, (next_id, state, resume_of, _, _)) = chat_runs_of(hub_db)
    assert (state, resume_of) == ("queued", spec["id"])
    assert [(m["name"], m["via"], m["run_id"], m["refusal"]) for m in follow_up["mentions"]] == [
        ("reviewer", chats.ANSWER, next_id, None)
    ]
    shown = thread(client, hub["owner"], chat["id"], mention["id"])
    assert follow_up["id"] in [m["id"] for m in shown], "it goes in the answer's thread"
    assert next(m for m in shown if m["id"] == posted["id"])["from_answer"] is None
    # an answer the chat does not carry, or a reply_to of another thread, is refused
    assert asked(client, hub["owner"], chat["id"], chart["id"] + 1000).status_code == 404
    elsewhere = client.post(f"/v1/chats/{chat['id']}/messages", json={"body": "a new topic"}, headers=hub["owner"])
    assert asked(client, hub["owner"], chat["id"], chart["id"], reply_to=elsewhere.json()["id"]).status_code == 422
    stranger_chat = created(client, hub["other"], title="Elsewhere")
    assert asked(client, hub["other"], stranger_chat["id"], chart["id"]).status_code == 404
    assert asked(client, hub["owner"], chat["id"], chart["id"], text=" ").status_code == 422
    long = "x" * (chats.MAX_ANSWER_PROMPT_CHARS + 1)
    assert asked(client, hub["owner"], chat["id"], chart["id"], text=long).status_code == 422


def test_follow_up_rate_limited(client, hub):
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    _, _, chart, _ = answered(client, hub, worker, chat)
    for n in range(chats.ANSWER_PROMPTS):
        response = asked(client, hub["owner"], chat["id"], chart["id"], text=f"week {n}")
        assert response.status_code == 201, response.text
    refused = asked(client, hub["owner"], chat["id"], chart["id"], text="one more week")
    assert refused.status_code == 429, refused.text
    assert refused.headers["retry-after"] == str(chats.ANSWER_WINDOW)
    # the limit is a person's: someone else still asks, and the owner still writes in the chat
    assert asked(client, hub["other"], chat["id"], chart["id"]).status_code == 201
    plain = client.post(f"/v1/chats/{chat['id']}/messages", json={"body": "written by hand"}, headers=hub["owner"])
    assert plain.status_code == 201, plain.text


def test_claimed_prompt_lets_cdns_load_only_in_unrestricted_chat(client, hub):
    """Criterion a5: the prompt the worker claims says, from whether the chat is restricted now, whether an
    interactive answer may load libraries from the CDNs."""
    worker = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"], people=[OTHER])
    _, spec = started(client, hub, worker, chat)
    assert chat_runs.INTERACTIVE_RULE in spec["prompt"]
    assert chat_runs.INTERACTIVE_CDN_RULE in spec["prompt"]
    assert chat_runs.INTERACTIVE_NO_CDN_RULE not in spec["prompt"]
    ended(client, worker, spec["id"])
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": CUSTOMER_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=hub["admin"]).status_code == 200
    assert client.get(f"/v1/chats/{chat['id']}", headers=hub["owner"]).json()["restricted"] is True
    _, restricted = started(client, hub, worker, chat, "@reviewer compare the two plans")
    assert chat_runs.INTERACTIVE_RULE in restricted["prompt"]
    assert chat_runs.INTERACTIVE_NO_CDN_RULE in restricted["prompt"]
    assert not any(host in restricted["prompt"] for host in outputs.CDN_HOSTS)
