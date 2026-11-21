"""Data loss prevention of a project (plan evo-creative-workspace, step 2, criterion a3; ``evo_agents.hub.dlp`` and
``evo_agents.hub.server.dlp_policy``).

The checks the step names: the rules find a CIF code, a national ID (CCCD), an account number, a confidential
financial figure and a confidential-contract keyword, and leave years, ports and phone numbers alone; a project's admin
alone sets its policy, which a reader reads and tries; the gateway route and the hub runtime apply it before a call
leaves for a provider, redacted or blocked with 422; dlp_counts counts each kind, and no matched value is stored or
logged."""

from __future__ import annotations

import json
import logging

import pytest

from evo_agents.hub import dlp

SECRET_CIF = "CIF 004512783"
SECRET_ID = "079203001234"
SECRET_ACCOUNT = "STK: 1903 4567 8901 23"
SECRET_FIGURE = "12.500.000.000 VND"
SAMPLE = (
    f"Customer {SECRET_CIF}, CCCD {SECRET_ID}, pays from {SECRET_ACCOUNT} the sum of {SECRET_FIGURE} under the "
    "hợp đồng mật of northwind."
)
VALUES = (SECRET_CIF, SECRET_ID, "1903 4567 8901 23", "12.500.000.000")


# The rules


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("see CIF:12345678 today", "cif"),
        ("the holder of CCCD 001099012345", "national_id"),
        ("id 036185000123 on file", "national_id"),
        ("CMND 123456789", "national_id"),
        ("account no. 0123-4567-8901", "account_number"),
        ("so tai khoan 19034567890123", "account_number"),
        ("a/c 12345678", "account_number"),
        ("a budget of $1,250,000", "financial_figure"),
        ("3 500 000 USD in fees", "financial_figure"),
        ("12000 trieu dong", "financial_figure"),
        ("marked Strictly   Confidential", "contract_keyword"),
        ("theo hợp đồng mật số 7", "contract_keyword"),
        ("TUYỆT MẬT", "contract_keyword"),
    ],
)
def test_each_rule_finds_its_kind(text, kind):
    found = dlp.apply(dlp.Policy("redact"), text)
    assert dict(found.counts) == {kind: 1}, found.counts
    assert f"[REDACTED:{kind}]" in found.text


@pytest.mark.parametrize(
    "text",
    [
        "released in 2026, served on port 8080",
        "call 0912345678 or +84 912 345 678",
        "the run 123456789012345678901234 failed",
        "an error code CIFX123",
        "it costs 12 USD",
        "a confidentiality clause",
        "commit 0790a3b001234",
    ],
)
def test_the_rules_leave_years_ports_and_phones_alone(text):
    assert dlp.apply(dlp.Policy("redact"), text).counts == {}


def test_redact_replaces_each_match_and_block_keeps_the_text():
    redacted = dlp.apply(dlp.Policy("redact"), SAMPLE)
    assert dict(redacted.counts) == {
        "cif": 1,
        "national_id": 1,
        "account_number": 1,
        "financial_figure": 1,
        "contract_keyword": 1,
    }
    assert not redacted.blocked and not any(value in redacted.text for value in VALUES)
    assert redacted.text.startswith("Customer [REDACTED:cif], [REDACTED:national_id], pays from")  # CCCD with it
    blocked = dlp.apply(dlp.Policy("block"), SAMPLE)
    assert blocked.blocked and blocked.text == SAMPLE and blocked.counts == redacted.counts
    assert dlp.apply(dlp.Policy("off"), SAMPLE).counts == {}
    assert dlp.apply(dlp.Policy("redact", kinds=("cif",)), SAMPLE).counts == {"cif": 1}
    own = dlp.apply(dlp.Policy("redact", kinds=("contract_keyword",), keywords=("Project Lumen",)), "re project  lumen")
    assert own.text == "re [REDACTED:contract_keyword]"


def test_a_json_body_keeps_its_routing_and_a_body_with_nothing_found_is_sent_as_given():
    body = {"model": "CIF 004512783", "messages": [{"role": "user", "content": SAMPLE, "name": "CIF 1234567"}]}
    made, scan = dlp.apply_json(dlp.Policy("redact"), body)
    assert made["model"] == body["model"] and made["messages"][0]["name"] == "CIF 1234567"
    assert "[REDACTED:cif]" in made["messages"][0]["content"] and scan.counts["cif"] == 1
    plain = b'{"model": "m",  "messages": []}'
    assert dlp.apply_bytes(dlp.Policy("redact"), plain) == (plain, dlp.Scan(""))
    sent, scan = dlp.apply_bytes(dlp.Policy("redact"), json.dumps(body).encode())
    assert SECRET_CIF not in json.loads(sent)["messages"][0]["content"] and scan.counts["cif"] == 1


# The policy, on the hub

from tests.hub import pg  # noqa: E402

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select  # noqa: E402

from evo_agents.hub import tables  # noqa: E402
from tests.hub import live  # noqa: E402
from tests.hub.live import sql  # noqa: E402
from tests.hub.test_chat_runs import created, sent  # noqa: E402
from tests.hub.test_credentials_api import leased  # noqa: E402
from tests.hub.test_gateway_runs import gateway_lease, gateway_run, new_daemon, provider_account  # noqa: E402
from tests.hub.test_hub_runtime import chat_runs_of, client, gateway, helper, run_row, turn  # noqa: E402,F401
from tests.hub.test_provider_accounts import PROJECT  # noqa: E402
from tests.hub.test_provider_accounts import hub as hub  # noqa: E402,F401  the fixture
from tests.hub.test_runs import audit_rows  # noqa: E402

POLICY = f"/v1/projects/{PROJECT}/dlp-policy"


def policy(client, headers, mode: str, **extra) -> dict:  # noqa: F811
    response = client.put(POLICY, json={"mode": mode, **extra}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def counts(db) -> list[tuple]:
    c = tables.dlp_counts
    return sql(db, select(c.c.kind, c.c.surface, c.c.action, c.c.count).order_by(c.c.kind, c.c.surface, c.c.action))


def test_the_admin_sets_the_policy_and_a_reader_reads_and_tries_it(client, hub, hub_db):  # noqa: F811
    shown = client.get(POLICY, headers=hub["reader"])
    assert shown.status_code == 200, shown.text
    assert (shown.json()["mode"], shown.json()["kinds"], shown.json()["counts"]) == ("off", list(dlp.KINDS), [])
    assert shown.json()["builtin_keywords"] == list(dlp.CONTRACT_KEYWORDS)
    for name in ("other", "reader"):
        assert client.put(POLICY, json={"mode": "block"}, headers=hub[name]).status_code == 403
    assert client.get(POLICY, headers=hub["stranger"]).status_code == 404
    assert client.put(POLICY, json={"mode": "loud"}, headers=hub["owner"]).status_code == 422
    assert client.put(POLICY, json={"mode": "block", "kinds": ["phone"]}, headers=hub["owner"]).status_code == 422

    made = policy(client, hub["owner"], "redact", kinds=["cif", "contract_keyword", "cif"], keywords=[" Lumen Deal "])
    assert (made["mode"], made["kinds"], made["keywords"]) == ("redact", ["cif", "contract_keyword"], ["Lumen Deal"])
    assert made["updated_at"] is not None
    (row,) = [row for row in audit_rows(hub_db, "dlp") if row[0] == "dlp.policy"]
    assert row[1] == f"{PROJECT} mode=redact kinds=2 keywords=1" and "Lumen" not in str(row)

    tried = client.post(f"/v1/projects/{PROJECT}/dlp/inspect", json={"text": SAMPLE}, headers=hub["reader"])
    assert tried.status_code == 200, tried.text
    assert tried.json()["findings"] == [{"kind": "cif", "count": 1}, {"kind": "contract_keyword", "count": 1}]
    assert tried.json()["mode"] == "redact" and not tried.json()["blocked"] and SECRET_CIF not in tried.json()["text"]
    assert client.post(f"/v1/projects/{PROJECT}/dlp/inspect", json={"text": SAMPLE}, headers=hub["stranger"])
    assert counts(hub_db) == []  # a quick check counts nothing
    assert SECRET_CIF not in live.table_dump(hub_db)


def relay_text(client, token: str, text: str):  # noqa: F811
    body = {"model": "openrouter/auto", "stream": True, "messages": [{"role": "user", "content": text}]}
    return client.post("/v1/messages", json=body, headers={"x-api-key": token, "anthropic-version": "2023-06-01"})


def test_the_gateway_route_sends_a_call_redacted_or_blocks_it_with_422(client, hub, hub_db, gateway, caplog):  # noqa: F811
    provider_account(client, hub["owner"])
    worker = new_daemon(client, hub_db, hub["owner"])
    run_id = gateway_run(client, hub_db, hub["owner"], worker)
    token = gateway_lease(leased(client, worker, run_id))["value"]
    assert relay_text(client, token, SAMPLE).status_code == 200
    assert SECRET_CIF in gateway.relayed[-1].content.decode()  # no policy: the call goes as it is

    policy(client, hub["owner"], "redact")
    caplog.set_level(logging.DEBUG)
    assert relay_text(client, token, SAMPLE).status_code == 200
    went = json.loads(gateway.relayed[-1].content)
    assert went["model"] == "openrouter/auto" and went["stream"] is True
    assert "[REDACTED:cif]" in went["messages"][0]["content"]
    assert not any(value in gateway.relayed[-1].content.decode() for value in VALUES)
    assert relay_text(client, token, "nothing of note").status_code == 200

    policy(client, hub["owner"], "block")
    before = len(gateway.relayed)
    refused = relay_text(client, token, f"see {SECRET_CIF} and {SECRET_FIGURE}")
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"] == "the project's DLP policy blocked this call: it holds 1 cif, 1 financial_figure"
    assert len(gateway.relayed) == before  # it never left
    assert counts(hub_db) == [
        ("account_number", "gateway_proxy", "redacted", 1),
        ("cif", "gateway_proxy", "blocked", 1),
        ("cif", "gateway_proxy", "redacted", 1),
        ("contract_keyword", "gateway_proxy", "redacted", 1),
        ("financial_figure", "gateway_proxy", "blocked", 1),
        ("financial_figure", "gateway_proxy", "redacted", 1),
        ("national_id", "gateway_proxy", "redacted", 1),
    ]
    shown = client.get(POLICY, headers=hub["reader"]).json()["counts"]
    assert {"kind": "cif", "surface": "gateway_proxy", "action": "blocked", "count": 1} in shown
    logged = caplog.text + json.dumps([record.__dict__ for record in caplog.records], default=str)
    assert not any(value in logged for value in VALUES)
    assert not any(value in live.table_dump(hub_db) for value in VALUES)


def test_the_hub_runtime_sends_a_turn_redacted_or_fails_it_when_blocked(client, hub, hub_db, gateway, helper):  # noqa: F811
    policy(client, hub["owner"], "redact")
    chat = created(client, hub["owner"], agents=["helper"])
    sent(client, hub["owner"], chat["id"], f"@helper is {SECRET_CIF} the holder of {SECRET_ACCOUNT}?")
    (first,) = chat_runs_of(hub_db)
    assert turn(client, gateway, first)["state"] == "done"
    asked = json.dumps(gateway.asked[0]["messages"])
    assert "[REDACTED:cif]" in asked and "[REDACTED:account_number]" in asked
    assert SECRET_CIF not in asked and gateway.asked[0]["tools"]

    policy(client, hub["owner"], "block")
    before = len(gateway.asked)
    sent(client, hub["owner"], chat["id"], f"@helper and {SECRET_CIF}?")
    (_, second) = chat_runs_of(hub_db)
    assert turn(client, gateway, second)["state"] == "failed"
    assert len(gateway.asked) == before
    assert run_row(hub_db, second)[0] == "failed"
    ((error,),) = sql(hub_db, select(tables.runs.c.error).where(tables.runs.c.id == second))
    # the thread it reads holds the first question too, as its member wrote it
    assert (
        error == "the hub runtime failed: the project's DLP policy blocked this call: it holds 1 account_number, 2 cif"
    )
    assert ("cif", "hub_runtime", "blocked", 2) in counts(hub_db)
    assert ("cif", "hub_runtime", "redacted", 1) in counts(hub_db)
