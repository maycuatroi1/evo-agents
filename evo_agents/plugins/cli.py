"""``evo-agents plugin test|pack|sign|keygen|market-check``: what the author of a plugin, or of a market, runs on
their own machine.

- ``test DIR``: the conformance checks of ``evo_agents.plugins.conformance``; exit 1 when one fails;
- ``pack DIR``: the plugin's deterministic bundle, ``<id>-<version>.tar.gz`` in the current directory or ``--output``,
  and its SHA-256;
- ``sign BUNDLE --key FILE``: an Ed25519 signature of the bundle, written beside it as ``BUNDLE.sig.json``, which
  ``evo-agents hub plugin publish`` picks up. A bundle a GitHub Actions workflow signs with Sigstore needs no command
  here: ``python -m sigstore sign BUNDLE`` writes ``BUNDLE.sigstore.json``;
- ``keygen --output FILE``: a new Ed25519 key, its 32-byte seed in base64 in FILE (mode 0600), and the
  EVO_HUB_PLUGIN_PUBLISHERS entry of its public key;
- ``market-check FILE [--releases]``: the index of a market, ``market.json``, against plugin-market-v1
  (``evo_agents.plugins.market``); with ``--releases``, the latest GitHub release of each free isolated plugin it
  lists carries the bundle and its signature, which the identity the index names signed. It reads GitHub at
  ``GITHUB_API_URL`` (default https://api.github.com) with ``GITHUB_TOKEN`` when set; exit 1 on any problem.

Signing imports ``cryptography``, which the hub-server extra brings. Standard library otherwise.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import sys
from pathlib import Path

from evo_agents.plugins.bundle import BundleError, pack, read_bundle
from evo_agents.plugins.signing import sign_ed25519

SEED_BYTES = 32


def _test(args) -> int:
    from evo_agents.plugins.conformance import run_conformance

    report = run_conformance(args.directory, timeout=args.timeout)
    if args.json:
        print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
        return 0 if report.ok else 1
    for check in report.checks:
        print(f"{'OK  ' if check.ok else 'FAIL'} {check.name}")
        for detail in check.details:
            print(f"       {detail}")
    failed = [check.name for check in report.checks if not check.ok]
    print(f"{len(report.checks)} check(s), {len(failed)} failed" + (f": {', '.join(failed)}" if failed else ""))
    return 0 if report.ok else 1


def _pack(args) -> int:
    try:
        bundle = pack(args.directory)
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    manifest = bundle.manifest
    output = Path(args.output) if args.output else Path(f"{manifest.id}-{manifest.version}.tar.gz")
    output.write_bytes(bundle.data)
    print(f"{output}  {manifest.id} {manifest.version}  {len(bundle.data)} bytes  sha256 {bundle.sha256}")
    return 0


def _read_seed(path: Path) -> bytes:
    try:
        seed = base64.b64decode(path.read_text(encoding="utf-8").strip(), validate=True)
    except (OSError, binascii.Error, ValueError) as exc:
        raise ValueError(f"cannot read the key in {path}: {exc}") from None
    if len(seed) != SEED_BYTES:
        raise ValueError(f"{path} holds {len(seed)} bytes, not the {SEED_BYTES}-byte seed of an Ed25519 key")
    return seed


def _sign(args) -> int:
    bundle = Path(args.bundle)
    try:
        data = bundle.read_bytes()
        read_bundle(data)
        envelope = sign_ed25519(data, _read_seed(Path(args.key)))
    except (OSError, BundleError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    output = Path(args.output) if args.output else Path(f"{bundle}.sig.json")
    output.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    print(f"{output}  ed25519  sha256 {envelope['sha256']}")
    return 0


def _keygen(args) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    output = Path(args.output)
    if output.exists():
        print(f"error: {output} exists; a key is never overwritten", file=sys.stderr)
        return 1
    seed = os.urandom(SEED_BYTES)
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(base64.b64encode(seed).decode() + "\n")
    print(f"wrote the private key to {output}; keep it out of every repository")
    print(f"EVO_HUB_PLUGIN_PUBLISHERS entry: {args.publisher}=ed25519:{base64.b64encode(public).decode()}")
    return 0


class _GitHubReleases:
    """The releases of GitHub repos over urllib, for ``market-check --releases``."""

    def __init__(self, api: str, token: str | None):
        self.api = api.rstrip("/")
        self.token = token

    def _open(self, url: str, accept: str, auth: bool = True):
        import urllib.request

        class _Redirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                new = super().redirect_request(req, fp, code, msg, headers, newurl)
                if new is not None:
                    new.remove_header("Authorization")  # an asset's storage refuses a GitHub token
                return new

        headers = {"Accept": accept, "User-Agent": "evo-agents-market-check", "X-GitHub-Api-Version": "2022-11-28"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        opener = urllib.request.build_opener(_Redirect())
        return opener.open(urllib.request.Request(url, headers=headers), timeout=60)

    def release(self, repo: str, tag: str | None) -> dict | None:
        import urllib.error
        import urllib.parse

        where = f"tags/{urllib.parse.quote(tag, safe='')}" if tag else "latest"
        try:
            with self._open(f"{self.api}/repos/{repo}/releases/{where}", "application/vnd.github+json") as answer:
                return json.loads(answer.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise OSError(f"GitHub answered {exc.code} for the releases of {repo}") from None

    def download(self, asset: dict) -> bytes:
        from evo_agents.plugins.bundle import MAX_BUNDLE

        with self._open(str(asset["url"]), "application/octet-stream") as answer:
            data = answer.read(MAX_BUNDLE + 1)
        if len(data) > MAX_BUNDLE:
            raise OSError(f"{asset.get('name')} is larger than a bundle may be")
        return data


def check_releases(market: dict, releases, sigstore=None) -> list[dict]:
    """For each free isolated plugin of ``market``, whether its latest release proves what the index says."""
    from evo_agents.plugins.market import ReleaseError, fetch_release
    from evo_agents.plugins.signing import SignatureError, SignatureUnavailable

    found = []
    for item in market["plugins"]:
        if item["tier"] != "isolated" or item["price"]["kind"] != "free":
            found.append({"id": item["id"], "ok": True, "skipped": "only a free isolated plugin has releases to check"})
            continue
        try:
            checked = fetch_release(releases, item, sigstore=sigstore)
        except (ReleaseError, SignatureError, SignatureUnavailable, OSError, ValueError) as exc:
            found.append({"id": item["id"], "ok": False, "problem": str(exc)})
            continue
        found.append(
            {"id": item["id"], "ok": True, "version": checked.manifest.version, "signed_by": checked.signed.signed_by}
        )
    return found


def _market_check(args) -> int:
    from evo_agents.plugins.market import MarketError, read_market

    try:
        market = read_market(Path(args.file).read_bytes())
        problems: list[str] = []
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except MarketError as exc:
        market, problems = None, exc.problems
    checks = []
    if market is not None and args.releases:
        api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
        checks = check_releases(market, _GitHubReleases(api, os.environ.get("GITHUB_TOKEN") or None))
    ok = not problems and all(check["ok"] for check in checks)
    if args.json:
        print(json.dumps({"ok": ok, "problems": problems, "releases": checks}, ensure_ascii=False, indent=2))
        return 0 if ok else 1
    for problem in problems:
        print(f"FAIL {problem}")
    if market is not None:
        print(f"OK   {args.file}: market {market['name']}, {len(market['plugins'])} plugin(s)")
    for check in checks:
        if not check["ok"]:
            print(f"FAIL {check['id']}: {check['problem']}")
        elif "skipped" in check:
            print(f"SKIP {check['id']}: {check['skipped']}")
        else:
            print(f"OK   {check['id']} {check['version']}, signed by {check['signed_by']}")
    return 0 if ok else 1


def register(sub) -> None:
    plugin = sub.add_parser("plugin", help="test, pack and sign a plugin on this machine")
    psub = plugin.add_subparsers(dest="command", required=True)
    test = psub.add_parser("test", help="check that a plugin directory speaks plugin/1 and packs into a bundle")
    test.add_argument("directory", metavar="DIR", help="the plugin directory, evo-plugin.json at its top")
    test.add_argument("--timeout", type=float, default=10.0, help="seconds the backend has for each answer")
    test.add_argument("--json", action="store_true", help="machine-readable output")
    test.set_defaults(func=_test)
    packed = psub.add_parser("pack", help="pack a plugin directory into its deterministic bundle")
    packed.add_argument("directory", metavar="DIR")
    packed.add_argument("--output", "-o", metavar="FILE", help="default: <id>-<version>.tar.gz here")
    packed.set_defaults(func=_pack)
    signed = psub.add_parser("sign", help="sign a bundle with an Ed25519 key, beside it as BUNDLE.sig.json")
    signed.add_argument("bundle", metavar="BUNDLE")
    signed.add_argument("--key", required=True, metavar="FILE", help="the key `plugin keygen` wrote")
    signed.add_argument("--output", "-o", metavar="FILE", help="default: BUNDLE.sig.json")
    signed.set_defaults(func=_sign)
    keygen = psub.add_parser("keygen", help="make an Ed25519 key for signing plugins")
    keygen.add_argument("--output", "-o", required=True, metavar="FILE", help="where the private key goes")
    keygen.add_argument("--publisher", default="PUBLISHER", help="the publisher id to print in the entry")
    keygen.set_defaults(func=_keygen)
    market = psub.add_parser("market-check", help="check a market's market.json, and with --releases its releases")
    market.add_argument("file", metavar="FILE", help="the market's index, market.json")
    market.add_argument("--releases", action="store_true", help="check the latest release of each free plugin")
    market.add_argument("--json", action="store_true", help="machine-readable output")
    market.set_defaults(func=_market_check)
