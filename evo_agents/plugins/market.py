"""A plugin market: ``market.json`` in a GitHub repo, read against
``evo_agents/harness/schemas/plugin-market.schema.json`` (the seam plugin-market-v1), and the GitHub Releases that
carry each plugin's bundle and signature.

``read_market`` takes the index as JSON bytes or as an object already parsed and gives it back checked, or raises
MarketError listing every problem: the schema's, then what a schema cannot say. Each plugin is listed once; a paid
plugin names a seller the index lists, whose keys' ids match their keys; a core plugin names the Python package that
ships it in a hub's image and is not sold by subscription (a core plugin is never installed from a market); a price
has at most two decimals; a market lists at most MAX_PLUGINS plugins and MAX_SELLERS sellers.

A release of plugin ``ID`` at version ``V`` carries the bundle ``ID-V.tar.gz`` (``evo-agents plugin pack``) and its
signature beside it, ``ID-V.tar.gz.sigstore.json`` (``python -m sigstore sign``) or ``ID-V.tar.gz.sig.json``
(``evo-agents plugin sign``). ``release_assets`` finds them in a release as the GitHub API describes it, and
``check_release`` checks what was downloaded: the bundle reads, its manifest is the entry's plugin by the entry's
publisher at the release's version, and the identity the index names for the publisher signed it.

A hub and the ``evo-agents plugin market-check`` command both read releases through a ``Releases`` (the latest
release of a repo, a release by tag, an asset's bytes), so the checks here never depend on how they reach GitHub.
Standard library only, besides ``evo_agents.schema`` and the bundle and signature modules of this package.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Protocol

from evo_agents.plugins.bundle import BundleError, read_bundle
from evo_agents.plugins.compat import SpecError, parse_spec
from evo_agents.plugins.manifest import Manifest
from evo_agents.plugins.signing import (
    Publisher,
    SigstoreCheck,
    Verified,
    _public_key,
    key_id,
    parse_publishers,
    read_signature,
    verify,
)
from evo_agents.schema import errors, validate

MARKET_FILE = "market.json"
SCHEMA_ID = "plugin-market-v1"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "harness" / "schemas" / "plugin-market.schema.json"
MAX_MARKET = 1024 * 1024  # bytes of market.json
MAX_PLUGINS = 2000
MAX_SELLERS = 50
DEFAULT_SOURCE = "maycuatroi1/evo-agents-market"  # the market every hub lists unless its operator says otherwise
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
SIGNATURE_SUFFIXES = (".sigstore.json", ".sig.json")  # the files beside a bundle that sign it, in order of preference
_TAG = re.compile(r"v?((0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]{1,40})?)")


class MarketError(ValueError):
    """An index that is not a market's: ``problems`` says each thing that is wrong, with where."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class ReleaseError(ValueError):
    """A release that does not carry, or does not prove, what the market says of a plugin."""


@cache
def schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def _cents(value) -> bool:
    return isinstance(value, (int, float)) and abs(round(value * 100) - value * 100) < 1e-6


def _semantics(data: dict) -> list[str]:
    problems: list[str] = []
    sellers = data.get("sellers") or []
    plugins = data.get("plugins") or []
    if len(sellers) > MAX_SELLERS:
        problems.append(f"sellers: at most {MAX_SELLERS}")
    if len(plugins) > MAX_PLUGINS:
        problems.append(f"plugins: at most {MAX_PLUGINS}")
    seller_ids: set[str] = set()
    for index, seller in enumerate(sellers):
        if seller["id"] in seller_ids:
            problems.append(f"sellers[{index}].id: {seller['id']} is listed twice")
        seller_ids.add(seller["id"])
        for number, key in enumerate(seller["keys"]):
            try:
                actual = key_id(_public_key(key["public_key"]))
            except ValueError as exc:
                problems.append(f"sellers[{index}].keys[{number}].public_key: {exc}")
                continue
            if actual != key["key_id"]:
                problems.append(
                    f"sellers[{index}].keys[{number}].key_id: the key's id is {actual}, not {key['key_id']}"
                )
    seen: set[str] = set()
    for index, plugin in enumerate(plugins):
        where = f"plugins[{index}]"
        if plugin["id"] in seen:
            problems.append(f"{where}.id: {plugin['id']} is listed twice")
        seen.add(plugin["id"])
        price = plugin["price"]
        if price["kind"] == "subscription":
            if not _cents(price["usd_per_hub_month"]):
                problems.append(f"{where}.price.usd_per_hub_month: at most two decimals")
            if price["seller"] not in seller_ids:
                problems.append(f"{where}.price.seller: {price['seller']} is not in sellers")
        if plugin["tier"] == "core":
            if "package" not in plugin:
                problems.append(f"{where}.package: a core plugin names the Python package that ships it in the image")
            if price["kind"] != "free":
                problems.append(f"{where}.price: a core plugin comes with the image and is not sold by subscription")
        elif "package" in plugin:
            problems.append(f"{where}.package: only a core plugin ships in a Python package")
        if "requires" in plugin:
            try:
                parse_spec(plugin["requires"])
            except SpecError as exc:
                problems.append(f"{where}.requires: {exc}")
        try:
            signer_publisher(plugin["publisher"]["id"], plugin["publisher"]["signer"])
        except ValueError as exc:
            problems.append(f"{where}.publisher.signer: {exc}")
    return problems


def check_market(data) -> list[str]:
    """Every problem of index ``data``, an object already parsed; empty when it is a market's."""
    issues = errors(validate(data, schema()))
    if issues:
        return [f"{issue.path or '<root>'}: {issue.message}" for issue in issues]
    return _semantics(data)


def read_market(source: bytes | str | dict) -> dict:
    """The index ``source``, checked; MarketError with every problem otherwise."""
    if isinstance(source, dict):
        data = source
    else:
        if len(source) > MAX_MARKET:
            raise MarketError([f"{MARKET_FILE} is at most {MAX_MARKET // 1024} KiB"])
        try:
            data = json.loads(source)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketError([f"{MARKET_FILE} is not JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise MarketError([f"{MARKET_FILE} holds a JSON object"])
    problems = check_market(data)
    if problems:
        raise MarketError(problems)
    return data


def entry(market: dict, plugin_id: str) -> dict | None:
    return next((item for item in market.get("plugins", ()) if item["id"] == plugin_id), None)


# Signers and publishers


def signer_publisher(publisher_id: str, signer: str) -> Publisher:
    """The publisher ``publisher_id`` signed for by ``signer`` alone (``sigstore:WORKFLOW`` or ``ed25519:KEY``);
    ValueError when the signer does not read."""
    found = parse_publishers(f"{publisher_id}={signer}")
    return found[0]


def signer_identity(signer: str) -> str:
    """How a signer reads among a publisher's identities (``Publisher.identities``)."""
    return signer_publisher("x", signer).identities()[0]


def default_signer(repo: str) -> str:
    """The signer of a repo whose workflow ``.github/workflows/ci.yml`` signs its releases, as the template has it."""
    return f"sigstore:{repo}/.github/workflows/ci.yml"


def merge_publishers(base: tuple[Publisher, ...], extra: list[tuple[str, str]]) -> tuple[Publisher, ...]:
    """``base`` with each ``(publisher, signer)`` of ``extra`` added to its publisher, a new one when it is not there;
    an extra signer that does not read is left out."""
    found = {publisher.id: publisher for publisher in base}
    for publisher_id, signer in extra:
        try:
            added = signer_publisher(publisher_id, signer)
        except ValueError:
            continue
        current = found.get(publisher_id, Publisher(publisher_id))
        found[publisher_id] = Publisher(
            publisher_id,
            tuple(dict.fromkeys((*current.sigstore, *added.sigstore))),
            tuple(dict.fromkeys((*current.ed25519, *added.ed25519))),
        )
    return tuple(found.values())


def trusts(publishers: tuple[Publisher, ...], publisher_id: str, signer: str) -> bool:
    """Whether ``publishers`` already trust ``signer`` to sign for ``publisher_id``."""
    publisher = next((item for item in publishers if item.id == publisher_id), None)
    return publisher is not None and signer_identity(signer) in publisher.identities()


# Releases


class Releases(Protocol):
    """How a reader reaches GitHub Releases: a release as the API describes it, or None when there is none."""

    def release(self, repo: str, tag: str | None) -> dict | None: ...

    def download(self, asset: dict) -> bytes: ...


@dataclass(frozen=True)
class ReleaseAssets:
    """The version a release publishes and the assets of its bundle and signature."""

    tag: str
    version: str
    bundle: dict
    signature: dict


@dataclass(frozen=True)
class CheckedRelease:
    tag: str
    manifest: Manifest
    data: bytes
    signature: dict
    signed: Verified


def tag_version(tag: str) -> str | None:
    """The version of a release tag, ``1.2.0`` or ``v1.2.0``; None when the tag names no version."""
    match = _TAG.fullmatch(tag or "")
    return match[1] if match else None


def release_assets(plugin_id: str, release: dict) -> ReleaseAssets:
    """The bundle of ``plugin_id`` in GitHub release ``release`` and its signature; ReleaseError otherwise."""
    tag = str(release.get("tag_name") or "")
    version = tag_version(tag)
    if version is None:
        raise ReleaseError(f"release {tag or '(untagged)'} does not name a version: tag it {plugin_id} as vX.Y.Z")
    assets = {item.get("name"): item for item in release.get("assets") or () if isinstance(item, dict)}
    name = f"{plugin_id}-{version}.tar.gz"
    if name not in assets:
        raise ReleaseError(f"release {tag} has no asset {name}: attach the bundle `evo-agents plugin pack` writes")
    signature = next((assets[name + suffix] for suffix in SIGNATURE_SUFFIXES if name + suffix in assets), None)
    if signature is None:
        raise ReleaseError(f"release {tag} has no signature of {name}: attach {name}.sigstore.json")
    return ReleaseAssets(tag, version, assets[name], signature)


def check_release(
    item: dict, assets: ReleaseAssets, data: bytes, signature: bytes, sigstore: SigstoreCheck | None = None
) -> CheckedRelease:
    """Release ``assets`` of market entry ``item`` as downloaded: the bundle is the entry's plugin at the release's
    version, by the entry's publisher, signed by the identity the entry names; ReleaseError (or SignatureError,
    SignatureUnavailable) otherwise."""
    try:
        contents = read_bundle(data)
    except BundleError as exc:
        raise ReleaseError(f"{assets.bundle.get('name')}: {exc}") from None
    manifest = contents.manifest
    if manifest.id != item["id"]:
        raise ReleaseError(f"the bundle of release {assets.tag} is plugin {manifest.id}, not {item['id']}")
    if manifest.version != assets.version:
        raise ReleaseError(f"release {assets.tag} carries version {manifest.version} of {item['id']}")
    publisher = item["publisher"]
    if manifest.publisher != publisher["id"]:
        raise ReleaseError(
            f"{item['id']} {manifest.version} names publisher {manifest.publisher}; the market says {publisher['id']}"
        )
    envelope = read_signature(signature)
    signed = verify(
        data, envelope, manifest.publisher, (signer_publisher(publisher["id"], publisher["signer"]),), sigstore
    )
    return CheckedRelease(assets.tag, manifest, data, envelope, signed)


def fetch_release(
    releases: Releases, item: dict, tag: str | None = None, sigstore: SigstoreCheck | None = None
) -> CheckedRelease:
    """The latest release of market entry ``item`` (or the one tagged ``tag``), downloaded and checked."""
    release = releases.release(item["repo"], tag)
    if release is None:
        raise ReleaseError(f"{item['repo']} has no release" + (f" tagged {tag}" if tag else ""))
    assets = release_assets(item["id"], release)
    return check_release(item, assets, releases.download(assets.bundle), releases.download(assets.signature), sigstore)
