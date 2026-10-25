"""A market the tests read: northwind's index with lumen's notes plugin, free and isolated, signed by lumen's release
workflow; a paid plugin of northwind's; a core plugin that ships in the image. ``FakeReleases`` serves GitHub releases
from memory for ``evo_agents.plugins.market.Releases``."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from evo_agents.plugins.signing import key_id
from tests.plugins.samples import SEED, bundle_of, public_key, sigstore_signature

LUMEN_REPO = "lumen-labs/lumen-notes"
LUMEN_WORKFLOW = f"{LUMEN_REPO}/.github/workflows/ci.yml"
LUMEN_SIGNER = f"sigstore:{LUMEN_WORKFLOW}"
LUMEN_IDENTITY = f"https://github.com/{LUMEN_WORKFLOW}@refs/tags/v1.0.0"


def market_index(**changes) -> dict:
    """The index of the northwind market, with ``changes`` at its root."""
    key = public_key(SEED)
    data = {
        "schema": "plugin-market-v1",
        "name": "northwind-market",
        "title": "Northwind market",
        "owner": {"name": "Northwind", "url": "https://github.com/northwind"},
        "sellers": [
            {
                "id": "northwind",
                "url": "https://licenses.northwind.test",
                "keys": [{"key_id": key_id(base64.b64decode(key)), "public_key": key}],
            }
        ],
        "plugins": [
            {
                "id": "lumen-notes",
                "name": "Lumen notes",
                "description": "Meeting notes for a project, with action items as tasks.",
                "industries": ["education"],
                "categories": ["documents"],
                "repo": LUMEN_REPO,
                "publisher": {"id": "lumen", "name": "Lumen Labs", "signer": LUMEN_SIGNER},
                "tier": "isolated",
                "price": {"kind": "free"},
                "icon": "notebook",
                "homepage": f"https://github.com/{LUMEN_REPO}",
                "license": "Apache-2.0",
            },
            {
                "id": "northwind-ledger",
                "name": "Northwind ledger",
                "description": "Invoices and time sheets for an outsourcing team.",
                "industries": ["software-outsourcing"],
                "categories": ["sales"],
                "repo": "northwind/ledger",
                "publisher": {"id": "northwind", "signer": "sigstore:northwind/ledger/.github/workflows/ci.yml"},
                "tier": "isolated",
                "price": {"kind": "subscription", "usd_per_hub_month": 49.5, "seller": "northwind"},
            },
            {
                "id": "crm",
                "name": "CRM",
                "description": "Leads, deals and the pipeline of a sales team.",
                "industries": ["sales"],
                "categories": ["sales"],
                "repo": "maycuatroi1/evo-agents-core-plugins",
                "publisher": {
                    "id": "evo",
                    "signer": "sigstore:maycuatroi1/evo-agents-core-plugins/.github/workflows/ci.yml",
                },
                "tier": "core",
                "package": "evo-agents-core-plugins",
                "price": {"kind": "free"},
            },
        ],
    }
    data.update(changes)
    return data


def lumen_release(base: Path, version: str = "1.0.0", *, identity: str | None = None, **changes) -> dict:
    """A GitHub release of lumen-notes at ``version``: the release as the API describes it, with ``files``, the
    bytes of each asset by its name."""
    fields = {"id": "lumen-notes", "version": version, "publisher": "lumen", "name": "Lumen notes", **changes}
    bundle = bundle_of(base, **fields)
    name = f"lumen-notes-{version}.tar.gz"
    signer = identity or f"https://github.com/{LUMEN_WORKFLOW}@refs/tags/v{version}"
    signature = json.dumps(sigstore_signature(bundle.data, identity=signer)).encode()
    files = {name: bundle.data, f"{name}.sigstore.json": signature}
    assets = [
        {"id": index + 1, "name": asset, "size": len(data), "url": f"https://api.github.test/assets/{asset}"}
        for index, (asset, data) in enumerate(files.items())
    ]
    return {"tag_name": f"v{version}", "assets": assets, "files": files, "sha256": bundle.sha256}


class FakeReleases:
    """Releases of repos from memory: ``releases[repo]`` lists them, the latest last."""

    def __init__(self, releases: dict[str, list[dict]]):
        self.releases = releases
        self.downloads: list[str] = []

    def release(self, repo: str, tag: str | None) -> dict | None:
        found = self.releases.get(repo) or []
        if tag is None:
            return found[-1] if found else None
        return next((item for item in found if item["tag_name"] == tag), None)

    def download(self, asset: dict) -> bytes:
        self.downloads.append(asset["name"])
        for releases in self.releases.values():
            for release in releases:
                if asset["name"] in release["files"]:
                    return release["files"][asset["name"]]
        raise OSError(f"no asset {asset['name']}")
