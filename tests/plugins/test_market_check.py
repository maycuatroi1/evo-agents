"""The index of a plugin market, market.json, against plugin-market-v1 (evo_agents/plugins/market.py), and the
releases it points at: ``evo-agents plugin market-check FILE [--releases]`` refuses a wrong index with every problem
and a release whose bundle or signature does not match what the index says."""

from __future__ import annotations

import argparse
import copy
import json

import pytest

from evo_agents.plugins import cli as plugin_cli
from evo_agents.plugins.market import (
    SCHEMA_FILE,
    MarketError,
    ReleaseError,
    check_market,
    fetch_release,
    merge_publishers,
    read_market,
    release_assets,
    schema,
    signer_publisher,
    tag_version,
    trusts,
)
from evo_agents.plugins.signing import SignatureError, parse_publishers
from tests.plugins.market_samples import LUMEN_REPO, LUMEN_SIGNER, FakeReleases, lumen_release, market_index
from tests.plugins.samples import FakeSigstore, ed25519_publishers
from tests.plugins.test_plugin_manifest import ANNOTATIONS, CHECKED, keywords


def test_market_schema_ships_with_the_harness_schemas_and_uses_what_the_validator_checks():
    assert SCHEMA_FILE.name == "plugin-market.schema.json" and SCHEMA_FILE.parent.name == "schemas"
    loaded = schema()
    assert loaded["$id"].endswith("/plugin-market-v1.json")
    assert loaded["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert keywords(loaded, set()) <= CHECKED | ANNOTATIONS


def test_market_index_of_the_samples_reads():
    data = read_market(json.dumps(market_index()).encode())
    assert [item["id"] for item in data["plugins"]] == ["lumen-notes", "northwind-ledger", "crm"]
    assert check_market(market_index()) == []


def _plugin(data: dict, index: int = 0) -> dict:
    return data["plugins"][index]


@pytest.mark.parametrize(
    "change, problem",
    [
        (lambda d: d.update(schema="plugin-market-v2"), "schema: must equal"),
        (lambda d: d.pop("owner"), "owner: is required"),
        (lambda d: _plugin(d).pop("industries"), "plugins[0].industries: is required"),
        (lambda d: _plugin(d).update(industries=[]), "plugins[0].industries: needs at least 1"),
        (lambda d: _plugin(d).update(description="x" * 281), "plugins[0].description: longer than 280"),
        (lambda d: _plugin(d).update(id="Lumen"), "plugins[0].id: does not match"),
        (lambda d: _plugin(d).update(repo="lumen-notes"), "plugins[0].repo: does not match"),
        (lambda d: _plugin(d)["publisher"].update(signer="sigstore:lumen"), "plugins[0].publisher.signer"),
        (lambda d: d["plugins"].append(copy.deepcopy(_plugin(d))), "plugins[3].id: lumen-notes is listed twice"),
        (lambda d: _plugin(d, 1)["price"].update(seller="lumen"), "plugins[1].price.seller: lumen is not in sellers"),
        (lambda d: _plugin(d, 1)["price"].update(usd_per_hub_month=9.999), "at most two decimals"),
        (lambda d: _plugin(d, 1)["price"].update(usd_per_hub_month=0), "plugins[1].price: does not match"),
        (lambda d: d["sellers"][0]["keys"][0].update(key_id="ed25519:0123456789abcdef"), "the key's id is"),
        (lambda d: _plugin(d, 2).pop("package"), "plugins[2].package: a core plugin names"),
        (lambda d: _plugin(d).update(package="lumen"), "plugins[0].package: only a core plugin"),
        (lambda d: _plugin(d).update(requires="~~1"), "plugins[0].requires"),
        (lambda d: _plugin(d).update(icon="http://lumen.test/i.png"), "plugins[0].icon: does not match"),
    ],
)
def test_market_index_that_is_wrong_is_refused_with_where(change, problem):
    data = market_index()
    change(data)
    with pytest.raises(MarketError) as raised:
        read_market(data)
    assert any(problem in item for item in raised.value.problems), raised.value.problems


def test_market_paid_core_plugin_is_refused():
    data = market_index()
    _plugin(data, 2)["price"] = {"kind": "subscription", "usd_per_hub_month": 10, "seller": "northwind"}
    assert any("not sold by subscription" in item for item in check_market(data))


def test_market_signer_reads_as_a_publisher_entry_does():
    publisher = signer_publisher("lumen", LUMEN_SIGNER)
    assert publisher.identities() == [LUMEN_SIGNER]
    with pytest.raises(ValueError):
        signer_publisher("lumen", "sigstore:lumen/notes")
    merged = merge_publishers(parse_publishers(ed25519_publishers()), [("lumen", LUMEN_SIGNER), ("acme", "bad")])
    assert [item.id for item in merged] == ["acme", "lumen"] and trusts(merged, "lumen", LUMEN_SIGNER)
    assert not trusts(merged, "acme", LUMEN_SIGNER)


def test_market_release_tags_and_assets(tmp_path):
    assert tag_version("v1.2.0") == "1.2.0" and tag_version("1.2.0-rc.1") == "1.2.0-rc.1"
    assert tag_version("latest") is None
    release = lumen_release(tmp_path)
    assets = release_assets("lumen-notes", release)
    assert assets.version == "1.0.0" and assets.signature["name"] == "lumen-notes-1.0.0.tar.gz.sigstore.json"
    with pytest.raises(ReleaseError, match="has no asset lumen-notes-1.0.0.tar.gz"):
        release_assets("lumen-notes", {**release, "assets": release["assets"][1:]})
    with pytest.raises(ReleaseError, match="no signature"):
        release_assets("lumen-notes", {**release, "assets": release["assets"][:1]})
    with pytest.raises(ReleaseError, match="does not name a version"):
        release_assets("lumen-notes", {**release, "tag_name": "nightly"})


def test_market_release_is_checked_against_the_entry(tmp_path):
    item = _plugin(market_index())
    sigstore = FakeSigstore()
    checked = fetch_release(FakeReleases({LUMEN_REPO: [lumen_release(tmp_path)]}), item, sigstore=sigstore)
    assert checked.manifest.id == "lumen-notes" and checked.signed.publisher == "lumen" and sigstore.calls == 1
    # signed by another workflow than the index names
    other = lumen_release(tmp_path / "other", identity="https://github.com/lumen-labs/fork/.github/workflows/ci.yml@x")
    with pytest.raises(SignatureError) as raised:
        fetch_release(FakeReleases({LUMEN_REPO: [other]}), item, sigstore=sigstore)
    assert raised.value.reason == "untrusted"
    # the bundle names another publisher
    wrong = lumen_release(tmp_path / "wrong", publisher="northwind")
    with pytest.raises(ReleaseError, match="names publisher northwind; the market says lumen"):
        fetch_release(FakeReleases({LUMEN_REPO: [wrong]}), item, sigstore=sigstore)
    with pytest.raises(ReleaseError, match="has no release"):
        fetch_release(FakeReleases({}), item, sigstore=sigstore)


def test_market_check_command(tmp_path, capsys, monkeypatch):
    path = tmp_path / "market.json"
    path.write_text(json.dumps(market_index()), encoding="utf-8")
    parser = argparse.ArgumentParser()
    plugin_cli.register(parser.add_subparsers(dest="top"))
    args = parser.parse_args(["plugin", "market-check", str(path)])
    assert args.func(args) == 0
    assert "market northwind-market, 3 plugin(s)" in capsys.readouterr().out

    bad = market_index()
    _plugin(bad)["industries"] = []
    path.write_text(json.dumps(bad), encoding="utf-8")
    args = parser.parse_args(["plugin", "market-check", str(path), "--json"])
    assert args.func(args) == 1
    assert json.loads(capsys.readouterr().out)["problems"] == ["plugins[0].industries: needs at least 1 item(s)"]

    path.write_text(json.dumps(market_index()), encoding="utf-8")
    releases = FakeReleases({LUMEN_REPO: [lumen_release(tmp_path / "r")]})
    monkeypatch.setattr(plugin_cli, "_GitHubReleases", lambda api, token: releases)
    monkeypatch.setattr(plugin_cli, "check_releases", _check)
    args = parser.parse_args(["plugin", "market-check", str(path), "--releases"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "OK   lumen-notes 1.0.0, signed by https://github.com/lumen-labs/lumen-notes" in out
    assert "SKIP northwind-ledger" in out and "SKIP crm" in out


_REAL_CHECK = plugin_cli.check_releases


def _check(market, releases):
    return _REAL_CHECK(market, releases, FakeSigstore())


def test_market_check_releases_reports_a_bad_release(tmp_path):
    other = lumen_release(tmp_path, identity="https://github.com/northwind/fork/.github/workflows/ci.yml@refs/x")
    found = plugin_cli.check_releases(market_index(), FakeReleases({LUMEN_REPO: [other]}), FakeSigstore())
    assert found[0]["id"] == "lumen-notes" and not found[0]["ok"]
    assert "does not verify" in found[0]["problem"] or "signed" in found[0]["problem"]
