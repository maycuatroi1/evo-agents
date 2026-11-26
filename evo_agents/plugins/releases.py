"""The releases of a market's plugins on GitHub, for ``evo-agents plugin market-check --releases``: :class:`GitHubReleases`
reads them over urllib and :func:`check_releases` says whether the latest release of each free isolated plugin proves
what the market's index says (``evo_agents.plugins.market.fetch_release``). Standard library only.
"""

from __future__ import annotations

import json


class GitHubReleases:
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
