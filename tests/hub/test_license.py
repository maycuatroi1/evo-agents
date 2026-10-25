"""The licence of a paid plugin (evo_agents.plugins.license) and the money of an order (evo_agents.hub.payments):
a licence a seller signed reads offline with the keys a market lists for it, and refuses what was changed, another
key, another hub or plugin; a month's revenue splits 80/20 to the cent (plan plugin-market, criterion a7)."""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal

import pytest

from evo_agents.hub import payments
from evo_agents.plugins import license as lic
from evo_agents.plugins.signing import key_id
from tests.plugins.market_samples import market_index
from tests.plugins.samples import OTHER_SEED, SEED

HUB = "hub_" + "ab" * 16


def granted(**changes) -> lic.License:
    fields = {
        "license": "lic_0123456789abcdef",
        "plugin": "northwind-ledger",
        "publisher": "northwind",
        "seller": "northwind",
        "hub": HUB,
        "order": "ORD-2026-0001",
        "key": key_id(base64.b64decode(lic.public_key_of(SEED))),
        "valid_from": date(2026, 1, 31),
        "valid_until": date(2026, 2, 28),
        "issued": "2026-01-31T09:00:00+00:00",
    }
    return lic.License(**{**fields, **changes})


def northwind_keys() -> dict[str, bytes]:
    return lic.seller_keys(market_index()["sellers"][0])


def test_a_license_the_seller_signed_reads_offline_with_the_keys_its_market_lists():
    token = lic.sign_license(granted(), SEED)
    assert lic.read_license(token, northwind_keys(), hub=HUB, plugin="northwind-ledger") == granted()
    assert lic.read_license(token, northwind_keys()).order == "ORD-2026-0001"


def test_a_license_refuses_another_key_a_changed_payload_another_hub_or_plugin_and_garbage():
    token = lic.sign_license(granted(), SEED)
    keys = northwind_keys()
    with pytest.raises(lic.LicenseError) as refused:
        lic.read_license(lic.sign_license(granted(key="ed25519:0000000000000000"), SEED), keys)
    assert refused.value.reason == "unknown_key"
    with pytest.raises(lic.LicenseError) as refused:  # signed with another key under northwind's key id
        lic.read_license(lic.sign_license(granted(), OTHER_SEED), keys)
    assert refused.value.reason == "invalid"
    payload, signature = token.split(".")
    changed = lic.canonical({**granted().payload(), "until": "2027-02-28"})
    forged = base64.urlsafe_b64encode(changed).rstrip(b"=").decode() + "." + signature
    with pytest.raises(lic.LicenseError) as refused:
        lic.read_license(forged, keys)
    assert refused.value.reason == "invalid"
    with pytest.raises(lic.LicenseError) as refused:
        lic.read_license(token, keys, hub="hub_" + "cd" * 16)
    assert refused.value.reason == "not_ours"
    with pytest.raises(lic.LicenseError) as refused:
        lic.read_license(token, keys, plugin="lumen-notes")
    assert refused.value.reason == "not_ours"
    for bad in ("", "abc", "a.b.c", payload, "x" * 5000, f"{payload}.!!", None):
        with pytest.raises(lic.LicenseError) as refused:
            lic.read_license(bad, keys)
        assert refused.value.reason == "malformed", bad
    extra = lic.canonical({**granted().payload(), "seats": "10"})
    with pytest.raises(lic.LicenseError):
        lic.read_license(base64.urlsafe_b64encode(extra).rstrip(b"=").decode() + "." + signature, keys)


def test_a_license_is_pending_active_in_grace_for_seven_days_then_lapsed():
    one = granted()
    assert one.state(date(2026, 1, 30)) == "pending"
    assert one.state(date(2026, 1, 31)) == one.state(date(2026, 2, 27)) == "active"
    assert one.state(date(2026, 2, 28)) == one.state(date(2026, 3, 6)) == "grace"
    assert one.state(date(2026, 3, 7)) == "lapsed"


def test_months_move_to_the_same_day_or_the_last_of_a_shorter_month():
    assert lic.add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert lic.add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert lic.add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert lic.add_months(date(2026, 5, 1), 36) == date(2029, 5, 1)


def test_a_seller_key_is_a_seed_and_the_public_key_its_market_lists_and_bad_keys_are_skipped():
    seed, public = lic.new_key()
    assert len(seed) == 32 and lic.public_key_of(seed) == public
    raw = base64.b64decode(public)
    seller = {"keys": [{"key_id": key_id(raw), "public_key": public}, {"key_id": "ed25519:x", "public_key": public}]}
    seller["keys"].append({"key_id": "ed25519:y", "public_key": "not a key"})
    assert lic.seller_keys(seller) == {key_id(raw): raw}
    assert lic.seller_keys({}) == {}


def test_revenue_splits_eighty_twenty_to_the_cent_and_adds_up():
    assert payments.split(Decimal("99")) == (Decimal("79.20"), Decimal("19.80"))
    assert payments.split("0.01") == (Decimal("0.00"), Decimal("0.01"))
    publisher, platform = payments.split(Decimal("148.51"))
    assert publisher == Decimal("118.80") and publisher + platform == Decimal("148.51")
    assert payments.usd(49.5) == Decimal("49.50")


def test_a_bank_transfer_hands_the_buyer_the_instructions_with_the_order_number_as_reference():
    seller = payments.provider("IBAN DE00 1234\nNorthwind GmbH")
    made = seller.create("ORD-2026-0001", Decimal("99.00"), {"hub": HUB})
    assert made.reference == "ORD-2026-0001" and made.provider == "manual"
    assert made.instructions == "IBAN DE00 1234\nNorthwind GmbH\nAmount: 99.00 USD. Reference: ORD-2026-0001."
    assert made.stored() == {"provider": "manual", "reference": "ORD-2026-0001"}
    assert seller.status(made.stored()) == "open"  # no gateway: a seller admin marks it paid
    assert payments.provider(None).instructions == payments.DEFAULT_INSTRUCTIONS
