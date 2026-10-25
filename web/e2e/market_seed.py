"""GitHub's side of the plugin market for the Playwright stack (e2e/market.spec.ts, plan plugin-market step 9).

The hub reads a market's ``market.json`` through GitHub's contents API, the latest release of each plugin through its
releases API, and the release's assets through the asset URL the release names, all at EVO_HUB_GITHUB_API_URL, the
stack's fake GitHub. ``MarketRepos`` serves those three from memory for the repos seeded here, and answers None for
any other path so the fake GitHub answers it as before.

POST /market/seed {market, repo, plugin, name, version} writes the index of the market repo ``market``: the free
isolated plugin ``plugin`` of ``repo``, named ``name`` and published by lumen, whose releases the Ed25519 key
``OTHER_SEED`` of the Python tests signs (``ed25519:KEY`` as its signer: Sigstore has no fake a browser run reaches);
``{plugin}-ledger``, a paid one of northwind's; and the core plugin notes, which e2e/hub_serve.py puts in the image.
It also publishes release ``v{version}`` of ``repo``: the sample plugin of tests/fixtures/plugins/sample packed as
``plugin`` at ``version`` by lumen, with its ``.sig.json``.
POST /market/release {repo, plugin, name, version} publishes another release of it, the latest from then on. Both answer
``{signer, tag}``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class MarketRepos:
    """The market files and plugin releases the fake GitHub serves, by repo."""

    def __init__(self):
        self._lock = threading.Lock()
        self.files: dict[tuple[str, str], bytes] = {}
        self.releases: dict[str, list[dict]] = {}
        self.assets: dict[str, bytes] = {}

    def answer(self, method: str, path: str, query: dict, headers: dict, base_url: str):
        """GitHub's answer for a market path, or None when ``path`` is not one this holds."""
        if method != "GET":
            return None
        if path.startswith("/market-assets/"):
            data = self.assets.get(path.removeprefix("/market-assets/"))
            return None if data is None else (200, data)
        parts = path.strip("/").split("/")
        if len(parts) < 5 or parts[0] != "repos":
            return None
        repo = f"{parts[1]}/{parts[2]}"
        with self._lock:
            if parts[3] == "contents" and any(known == repo for known, _ in self.files):
                data = self.files.get((repo, "/".join(parts[4:])))
                if data is None:
                    return 404, {"message": "Not Found"}
                etag = f'"{hashlib.sha256(data).hexdigest()[:20]}"'
                if headers.get("if-none-match") == etag:
                    return 304, None, {"ETag": etag}
                content = {"content": base64.b64encode(data).decode(), "encoding": "base64", "sha": etag.strip('"')}
                return 200, content, {"ETag": etag}
            if parts[3] == "releases" and repo in self.releases:
                found = self.releases[repo]
                if parts[4:] == ["latest"]:
                    release = found[-1] if found else None
                elif parts[4:5] == ["tags"] and len(parts) == 6:
                    release = next((item for item in found if item["tag_name"] == parts[5]), None)
                else:
                    release = None
                if release is None:
                    return 404, {"message": "Not Found"}
                return 200, {"tag_name": release["tag_name"], "assets": _assets(release["names"], base_url)}
        return None

    def seed(self, body: dict) -> dict:
        from evo_agents.plugins.signing import key_id
        from tests.plugins.samples import OTHER_SEED, SEED, public_key

        market = str(body.get("market") or "northwind/market")
        repo = str(body.get("repo") or "lumen-labs/lumen-notes")
        plugin = str(body.get("plugin") or "lumen-notes")
        name = str(body.get("name") or "Lumen notes")
        signer = f"ed25519:{public_key(OTHER_SEED)}"
        key = public_key(SEED)
        index = {
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
                    "id": plugin,
                    "name": name,
                    "description": "Meeting notes for a project, with action items as tasks.",
                    "industries": ["education"],
                    "categories": ["documents"],
                    "repo": repo,
                    "publisher": {"id": "lumen", "name": "Lumen Labs", "signer": signer},
                    "tier": "isolated",
                    "price": {"kind": "free"},
                    "homepage": f"https://github.com/{repo}",
                    "license": "Apache-2.0",
                },
                {
                    "id": f"{plugin}-ledger",
                    "name": "Northwind ledger",
                    "description": "Invoices and time sheets for an outsourcing team.",
                    "industries": ["software-outsourcing"],
                    "categories": ["sales"],
                    "repo": "northwind/ledger",
                    "publisher": {
                        "id": "northwind",
                        "name": "Northwind",
                        "signer": "sigstore:northwind/ledger/.github/workflows/ci.yml",
                    },
                    "tier": "isolated",
                    "price": {"kind": "subscription", "usd_per_hub_month": 49.5, "seller": "northwind"},
                },
                {
                    "id": "notes",
                    "name": "Notes",
                    "description": "Notes of a project, kept in the hub's own database.",
                    "industries": ["education", "software-outsourcing"],
                    "categories": ["documents"],
                    "repo": "northwind/notes",
                    "publisher": {
                        "id": "evo",
                        "name": "Evo",
                        "signer": "sigstore:northwind/notes/.github/workflows/ci.yml",
                    },
                    "tier": "core",
                    "package": "evo-notes",
                    "price": {"kind": "free"},
                },
            ],
        }
        with self._lock:
            self.files[(market, "market.json")] = json.dumps(index).encode()
            self.releases[repo] = []
        release = {"repo": repo, "plugin": plugin, "name": name, "version": body.get("version")}
        return {"signer": signer, **self.release(release)}

    def release(self, body: dict) -> dict:
        """Release ``version`` of ``plugin`` in ``repo``, packed from the sample plugin and signed by lumen's key."""
        from evo_agents.plugins.bundle import pack
        from tests.plugins.samples import OTHER_SEED, ed25519_signature

        repo = str(body.get("repo") or "lumen-labs/lumen-notes")
        plugin = str(body.get("plugin") or "lumen-notes")
        version = str(body.get("version") or "1.0.0")
        name = str(body.get("name") or "Lumen notes")
        with tempfile.TemporaryDirectory(prefix="e2e-market-") as root:
            directory = Path(root) / "plugin"
            shutil.copytree(ROOT / "tests" / "fixtures" / "plugins" / "sample", directory)
            manifest_path = directory / "evo-plugin.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(id=plugin, version=version, publisher="lumen", name=name)
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            bundle = pack(directory)
        name = f"{plugin}-{version}.tar.gz"
        signature = json.dumps(ed25519_signature(bundle.data, OTHER_SEED)).encode()
        tag = f"v{version}"
        with self._lock:
            self.assets[name] = bundle.data
            self.assets[f"{name}.sig.json"] = signature
            self.releases.setdefault(repo, []).append({"tag_name": tag, "names": [name, f"{name}.sig.json"]})
        return {"tag": tag}


def _assets(names: list[str], base_url: str) -> list[dict]:
    return [
        {"id": index + 1, "name": name, "url": f"{base_url}/market-assets/{name}"} for index, name in enumerate(names)
    ]
