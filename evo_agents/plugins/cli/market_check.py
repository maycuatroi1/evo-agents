"""``evo-agents plugin market-check FILE [--releases]``: the index of a market, ``market.json``, against
plugin-market-v1 (``evo_agents.plugins.market``); with ``--releases``, the latest GitHub release of each free isolated
plugin it lists carries the bundle and its signature, which the identity the index names signed
(``evo_agents.plugins.releases``). It reads GitHub at ``GITHUB_API_URL`` (default https://api.github.com) with
``GITHUB_TOKEN`` when set; exit 1 on any problem."""

from __future__ import annotations

import os
from pathlib import Path

from evo_agents.clikit import Arg, Context, Result, Text
from evo_agents.plugins import releases
from evo_agents.plugins.cli.base import JSON, PluginCommand, Refused


def check_lines(file: str, market: dict | None, problems: list[str], checks: list[dict]) -> list[str]:
    """What the terminal prints of a check of ``file``."""
    lines = [f"FAIL {problem}" for problem in problems]
    if market is not None:
        lines.append(f"OK   {file}: market {market['name']}, {len(market['plugins'])} plugin(s)")
    for check in checks:
        if not check["ok"]:
            lines.append(f"FAIL {check['id']}: {check['problem']}")
        elif "skipped" in check:
            lines.append(f"SKIP {check['id']}: {check['skipped']}")
        else:
            lines.append(f"OK   {check['id']} {check['version']}, signed by {check['signed_by']}")
    return lines


class MarketCheck(PluginCommand):
    name = "market-check"
    help = "check a market's market.json, and with --releases its releases"
    arguments = (
        Arg("file", metavar="FILE", help="the market's index, market.json"),
        Arg("--releases", action="store_true", help="check the latest release of each free plugin"),
        JSON,
    )

    def run(self, ctx: Context) -> Result:
        from evo_agents.plugins.market import MarketError, read_market

        try:
            market = read_market(Path(ctx.args.file).read_bytes())
            problems: list[str] = []
        except OSError as exc:
            raise Refused(str(exc)) from None
        except MarketError as exc:
            market, problems = None, exc.problems
        checks = []
        if market is not None and ctx.args.releases:
            api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
            checks = releases.check_releases(
                market, releases.GitHubReleases(api, os.environ.get("GITHUB_TOKEN") or None)
            )
        ok = not problems and all(check["ok"] for check in checks)
        data = {"ok": ok, "problems": problems, "releases": checks}
        return Result(data, Text(*check_lines(ctx.args.file, market, problems, checks)), 0 if ok else 1)


MARKET_CHECK = MarketCheck()
