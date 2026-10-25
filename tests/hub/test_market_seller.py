"""Paid plugins between two hubs (plan plugin-market, criterion a7): a seller's hub lists a plugin and uploads its
signed bundle; a buying hub orders it from the seller its market names, a seller admin marks the order paid with an
invoice number, which issues a licence; the buyer receives it at its next check, checks it offline and installs the
bundle the seller serves for that licence alone. Then the reminder before the licence ends, the lapse after its grace,
the renewal that turns the plugin on again, the month's revenue split 80/20 and its payout, the seller's signals, the
audit trail, and the refusals: the feature off, a licence of another hub, a wrong secret, no key to seal with.

Each hub has a database of its own (the buyer ``hub_db``, the seller a clone of the same template), and the buyer
reaches the seller through an ``httpx.MockTransport`` that hands each request to the seller's TestClient
(``app.state.seller_transport``). It skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import base64
import secrets
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import live, pg
from tests.hub.test_market import MarketGitHub, add_market, make_app
from tests.plugins.samples import SEED, bundle_of, sigstore_signature

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
pytest.importorskip("cryptography")

PLUGIN = "northwind-ledger"
SIGNER = "sigstore:northwind/ledger/.github/workflows/ci.yml"
INSTRUCTIONS = "Pay to Northwind Bank, account 0042."
CONTACT = {"name": "Octo Admin", "email": "octo@buyer.test"}
KEY = secrets.token_bytes(32)


def forward(target):
    """A transport that hands each request to TestClient ``target``."""
    import httpx

    def answer(request):
        headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
        got = target.request(request.method, request.url.path, headers=headers, content=request.content)
        return httpx.Response(got.status_code, content=got.content, headers={"content-type": "application/json"})

    return httpx.MockTransport(answer)


@contextmanager
def two_hubs(hub_db, tmp_path, s3, request, *, buyer=None, seller=None):
    from fastapi.testclient import TestClient

    seller_db = pg.clone_database(request.getfixturevalue("hub_template"))
    try:
        seller_changes = {
            "seller_id": "northwind",
            "seller_key": SEED,
            "seller_payment_instructions": INSTRUCTIONS,
            "seller_signal_project": "demo",
            **(seller or {}),
        }
        buyer_changes = {"secrets_key": KEY, "public_url": "https://buyer.test", **(buyer or {})}
        seller_app = make_app(seller_db, tmp_path / "seller", s3, MarketGitHub(), **seller_changes)
        buyer_app = make_app(hub_db, tmp_path / "buyer", s3, MarketGitHub(), **buyer_changes)
        with ExitStack() as stack:
            seller_client = stack.enter_context(TestClient(seller_app))
            buyer_client = stack.enter_context(TestClient(buyer_app))
            buyer_app.state.seller_transport = forward(seller_client)
            seller_who = {login: live.bearer(live.insert_token(seller_db, login)) for login in (live.ADMIN, "alice")}
            buyer_who = {login: live.bearer(live.insert_token(hub_db, login)) for login in (live.ADMIN, "bob")}
            yield SimpleNamespace(
                seller=SimpleNamespace(client=seller_client, app=seller_app, db=seller_db, who=seller_who),
                buyer=SimpleNamespace(client=buyer_client, app=buyer_app, db=hub_db, who=buyer_who),
                tmp=tmp_path,
            )
        pg.wait_no_backends(seller_db.name)
    finally:
        pg.drop_database(seller_db)


@pytest.fixture
def hubs(hub_db, tmp_path, s3, request):
    with two_hubs(hub_db, tmp_path, s3, request) as found:
        yield found


def call(side, method: str, path: str, login: str = live.ADMIN, status: int = 200, **kwargs) -> dict:
    response = side.client.request(method, path, headers=side.who[login], **kwargs)
    assert response.status_code == status, response.text
    return response.json()


def turn_on(side) -> None:
    call(side, "POST", "/v1/admin/features", json={"values": {"seller": True}})


def signed_bundle(base, version: str = "1.0.0") -> tuple[bytes, dict]:
    bundle = bundle_of(base, id=PLUGIN, version=version, publisher="northwind", name="Northwind ledger")
    identity = f"https://github.com/northwind/ledger/.github/workflows/ci.yml@refs/tags/v{version}"
    return bundle.data, sigstore_signature(bundle.data, identity=identity)


def list_for_sale(hubs) -> dict:
    turn_on(hubs.seller)
    body = {"plugin": PLUGIN, "publisher": "northwind", "signer": SIGNER, "usd_per_hub_month": "49.50"}
    listing = call(hubs.seller, "POST", "/v1/seller/listings", "alice", 201, json=body)
    assert (listing["state"], listing["owner"], listing["usd_per_hub_month"]) == ("draft", "alice", 49.5)
    data, signature = signed_bundle(hubs.tmp / "bundle")
    upload = {"bundle": base64.b64encode(data).decode(), "signature": signature}
    return call(hubs.seller, "POST", f"/v1/seller/listings/{PLUGIN}/bundles", "alice", 201, json=upload)


def buy(hubs, source: int, months: int, status: int = 201, **body) -> dict:
    payload = {"months": months, "contact": CONTACT, **body}
    return call(hubs.buyer, "POST", f"/v1/market/{source}/{PLUGIN}/buy", json=payload, status=status)


def check(hubs) -> dict:
    return call(hubs.buyer, "POST", "/v1/market/licenses/check")


def actions(db) -> list[str]:
    from sqlalchemy import select

    from evo_agents.hub import tables

    a = tables.audit
    query = select(a.c.action).where(a.c.action.like("market.%")).order_by(a.c.id)
    return [row[0] for row in live.sql(db, query)]


def plugin_row(hubs) -> dict:
    return call(hubs.buyer, "GET", f"/v1/plugins/{PLUGIN}")


@needs_pg
def test_a_hub_buys_a_paid_plugin_installs_it_with_its_licence_and_it_lapses_and_renews(hubs, monkeypatch):
    from sqlalchemy import func, insert, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server import market_licenses
    from evo_agents.plugins.license import add_months

    project_id = live.add_project(hubs.seller.db, "demo")
    sink = {
        "project_id": project_id,
        "sink_id": "hub",
        "kind": "hub",
        "clearance": {"level": "internal", "location": "any"},
    }
    live.sql(hubs.seller.db, insert(tables.project_sinks).values(**sink))
    call(
        hubs.seller,
        "PUT",
        "/v1/admin/projects/demo/grants/" + live.ADMIN,
        json={"role": "admin", "max_level": "internal"},
    )
    listing = list_for_sale(hubs)
    assert listing["state"] == "listed" and [b["version"] for b in listing["bundles"]] == ["1.0.0"]
    info = call(hubs.seller, "GET", "/v1/seller")
    assert info["selling"] is True and info["seller"] == "northwind" and info["key_id"]
    assert base64.b64encode(SEED).decode() not in str(info)

    source = add_market(hubs.buyer)["id"]
    hub = call(hubs.buyer, "GET", "/v1/market/hub")["hub"]
    refused = buy(hubs, source, 2, status=409)
    assert refused["message"].startswith("untrusted publisher")
    bought = buy(hubs, source, 2, trust_publisher=True)
    number = bought["purchase"]["number"]
    assert number.startswith(f"ORD-{datetime.now(UTC).year}-") and bought["purchase"]["state"] == "open"
    assert (
        INSTRUCTIONS in bought["instructions"] and f"Amount: 99.00 USD. Reference: {number}." in bought["instructions"]
    )
    assert bought["trusted_publisher"] is True

    orders = call(hubs.seller, "GET", "/v1/seller/orders", params={"state": "open"})["orders"]
    assert [(o["number"], o["buyer_hub"], o["buyer_url"], o["months"], o["usd_total"]) for o in orders] == [
        (number, hub, "https://buyer.test", 2, 99.0)
    ]
    assert check(hubs)["received"] == []

    paid = call(hubs.seller, "POST", f"/v1/seller/orders/{number}/paid", json={"invoice": "INV-0001"})
    today = datetime.now(UTC).date()
    assert paid["state"] == "paid" and paid["invoice"] == "INV-0001" and paid["license_id"].startswith("lic_")
    assert (paid["valid_from"], paid["valid_until"]) == (today.isoformat(), add_months(today, 2).isoformat())
    assert call(hubs.seller, "POST", f"/v1/seller/orders/{number}/paid", json={"invoice": "x"}, status=409)

    done = check(hubs)
    assert done["received"] == [paid["license_id"]] and done["installed"] == [PLUGIN] and done["errors"] == []
    assert plugin_row(hubs)["installed_version"] == "1.0.0"
    licences = call(hubs.buyer, "GET", "/v1/market/licenses")
    assert licences["hub"] == hub
    assert [(p["number"], p["state"], p["license_state"]) for p in licences["purchases"]] == [
        (number, "paid", "active")
    ]
    assert "token" not in str(licences) and "secret" not in licences["purchases"][0]

    # A week before the end: one notice, once.
    until = add_months(today, 2)
    monkeypatch.setattr(market_licenses, "today_utc", lambda: until - timedelta(days=3))
    assert check(hubs)["reminded"] == [PLUGIN]
    assert check(hubs)["reminded"] == []
    n = tables.notifications
    kinds = live.sql(hubs.buyer.db, select(n.c.notice_kind).where(n.c.notice_kind == "market_license_expiring"))
    assert kinds == [("market_license_expiring",)]

    # Seven days after the end: off everywhere, its config kept; the store does not turn it on again.
    monkeypatch.setattr(market_licenses, "today_utc", lambda: until + timedelta(days=7))
    assert check(hubs)["lapsed"] == [PLUGIN]
    row = plugin_row(hubs)
    assert row["state"] == "disabled" and row["disabled_reason"].startswith("licence lapsed")
    refused = call(hubs.buyer, "POST", f"/v1/plugins/{PLUGIN}/install", json={}, status=409)
    assert "no licence of this hub covers today" in str(refused)

    # A renewal runs on from the end of the last licence and turns the plugin on again.
    renewal = buy(hubs, source, 1)["purchase"]["number"]
    renewed = call(hubs.seller, "POST", f"/v1/seller/orders/{renewal}/paid", json={"invoice": "INV-0002"})
    assert (renewed["valid_from"], renewed["valid_until"]) == (until.isoformat(), add_months(until, 1).isoformat())
    done = check(hubs)
    assert done["received"] == [renewed["license_id"]] and done["installed"] == [PLUGIN]
    row = plugin_row(hubs)
    assert row["state"] == "installed" and row["disabled_reason"] is None

    # The month's revenue, split 80/20, and its payout once the month is over.
    month = today.strftime("%Y-%m")
    revenue = call(hubs.seller, "GET", "/v1/seller/revenue", params={"month": month})
    assert [
        (p["publisher"], p["orders"], p["gross_usd"], p["publisher_usd"], p["platform_usd"])
        for p in revenue["publishers"]
    ] == [("northwind", 2, 148.5, 118.8, 29.7)]
    payout = {"month": month, "publisher": "northwind"}
    assert "not over yet" in str(call(hubs.seller, "POST", "/v1/seller/payouts", json=payout, status=409))
    last = (today.replace(day=1) - timedelta(days=1)).replace(day=15)
    o = tables.market_orders
    live.sql(hubs.seller.db, update(o).values(closed_at=datetime(last.year, last.month, 15, tzinfo=UTC)))
    payout = {"month": last.strftime("%Y-%m"), "publisher": "northwind", "note": "wired 2026-10-02"}
    paid_out = call(hubs.seller, "POST", "/v1/seller/payouts", json=payout)
    assert paid_out["publishers"][0]["paid_out"]["publisher_usd"] == 118.8
    assert "already" in str(call(hubs.seller, "POST", "/v1/seller/payouts", json=payout, status=409))

    # The seller's signals, both keys registered in its project.
    sk, s = tables.signal_keys, tables.signals
    keys = live.sql(hubs.seller.db, select(sk.c.name, sk.c.kind).order_by(sk.c.name))
    assert keys == [("market.active_licenses", "number"), ("market.revenue_usd_month", "number")]
    assert live.sql(hubs.seller.db, select(func.count()).select_from(s))[0][0] >= 2

    assert actions(hubs.seller.db) == [
        "market.listing.add",
        "market.bundle.upload",
        "market.order.create",
        "market.order.paid",
        "market.license.issue",
        "market.order.create",
        "market.order.paid",
        "market.license.issue",
        "market.payout",
    ]
    assert actions(hubs.buyer.db) == [
        "market.source.add",
        "market.order.place",
        "market.license.receive",
        "market.license.lapse",
        "market.order.place",
        "market.license.receive",
    ]
    secret = base64.b64encode(SEED).decode()
    a = tables.audit
    assert all(secret not in target for (target,) in live.sql(hubs.seller.db, select(a.c.target)))


@needs_pg
def test_the_seller_serves_a_bundle_only_for_a_paid_licence_of_the_hub_that_asks(hubs):
    turned_off = hubs.seller.client.get("/v1/seller-api/info")
    assert turned_off.status_code == 404
    assert hubs.seller.client.get("/v1/seller/listings", headers=hubs.seller.who["alice"]).status_code == 404
    list_for_sale(hubs)
    data, signature = signed_bundle(hubs.tmp / "other", "1.1.0")
    upload = {"bundle": base64.b64encode(data).decode(), "signature": signature}
    call(hubs.seller, "POST", f"/v1/seller/listings/{PLUGIN}/bundles", "alice", 201, json=upload)
    bob = live.bearer(live.insert_token(hubs.seller.db, "bob"))
    refused = hubs.seller.client.post(f"/v1/seller/listings/{PLUGIN}/bundles", json=upload, headers=bob)
    assert refused.status_code == 403
    tampered = {
        **upload,
        "signature": sigstore_signature(
            data, identity="https://github.com/evil/x/.github/workflows/ci.yml@refs/tags/v1"
        ),
    }
    tampered["bundle"] = base64.b64encode(signed_bundle(hubs.tmp / "third", "1.2.0")[0]).decode()
    assert (
        hubs.seller.client.post(
            f"/v1/seller/listings/{PLUGIN}/bundles", json=tampered, headers=hubs.seller.who["alice"]
        ).status_code
        == 422
    )

    source = add_market(hubs.buyer)["id"]
    number = buy(hubs, source, 1, trust_publisher=True)["purchase"]["number"]
    assert (
        hubs.seller.client.get(
            f"/v1/seller-api/orders/{number}", headers={"X-Evo-Order-Secret": "not-the-secret"}
        ).status_code
        == 404
    )
    call(hubs.seller, "POST", f"/v1/seller/orders/{number}/paid", json={"invoice": "INV-7"})
    check(hubs)
    assert plugin_row(hubs)["installed_version"] == "1.1.0"

    from sqlalchemy import select

    from evo_agents.hub import tables

    p = tables.market_purchases
    ((token,),) = live.sql(hubs.buyer.db, select(p.c.token))
    ((hub,),) = live.sql(hubs.buyer.db, select(tables.hub_identity.c.hub_id))
    served = hubs.seller.client.get(
        f"/v1/seller-api/bundles/{PLUGIN}/1.0.0", headers={"Authorization": f"License {token}", "X-Evo-Hub-Id": hub}
    )
    assert served.status_code == 200 and served.json()["version"] == "1.0.0"
    other = "hub_" + "0" * 32
    for headers, status in (
        ({"X-Evo-Hub-Id": hub}, 401),
        ({"Authorization": f"License {token}", "X-Evo-Hub-Id": other}, 403),
        ({"Authorization": "License not.alicence", "X-Evo-Hub-Id": hub}, 403),
    ):
        assert hubs.seller.client.get(f"/v1/seller-api/bundles/{PLUGIN}", headers=headers).status_code == status


@needs_pg
def test_buying_needs_the_hubs_sealing_key_and_selling_needs_the_sellers_key(hub_db, tmp_path, s3, request):
    with two_hubs(hub_db, tmp_path, s3, request, buyer={"secrets_key": None}, seller={"seller_key": None}) as found:
        turn_on(found.seller)
        assert found.seller.client.get("/v1/seller-api/info").status_code == 503
        assert call(found.seller, "GET", "/v1/seller")["selling"] is False
        source = add_market(found.buyer)["id"]
        refused = buy(found, source, 1, status=503)
        assert "EVO_HUB_SECRETS_KEY" in str(refused)
