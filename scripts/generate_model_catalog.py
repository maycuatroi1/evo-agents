"""Regenerate evo_agents/hub/model_catalog.json from a tag of OmniRoute (MIT, github.com/cheaper-inference/OmniRoute).

    python scripts/generate_model_catalog.py v3.8.52
    python scripts/generate_model_catalog.py v3.8.52 --source /path/to/omniroute-checkout   # a checkout already there

How the source is read without installing OmniRoute's dependencies: the provider data is TypeScript that imports
other TypeScript, so the script clones the tag (depth 1), bundles scripts/omniroute/extract.ts with esbuild in a
scratch directory (esbuild and zod, the one package the bundle imports, are the only npm installs) and runs the bundle
with node. The bundle prints one JSON row per provider; ``build`` below, a pure function, turns the rows into the
catalog, so the filtering is tested without Node. Needs git, node and npm on the PATH.

The version of the gateway image step 3 pins is evo_agents.hub.model_catalog.GATEWAY_VERSION; a test fails when the
catalog is of another version, so an upgrade is one change: this script on the new tag, and that constant. The logos
of the providers follow the catalog: map its new ids in scripts/generate_provider_logos.py and run that script too (a
test fails on a provider it does not map).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/cheaper-inference/OmniRoute.git"
ESBUILD = "esbuild@0.25.12"
ZOD = "zod@3"
HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "evo_agents" / "hub" / "model_catalog.json"

API_FORMATS = {"openai": "openai", "claude": "anthropic", "openai-responses": "openai-responses", "gemini": "gemini"}
# executors that speak a protocol of their own or need more than a base URL and a key (cloud accounts, cookies)
BAD_EXECUTORS = frozenset(
    {
        "vertex",
        "bedrock",
        "cloudflare-ai",
        "clova-studio",
        "command-code",
        "cursor-api",
        "freebuff",
        "openai-compatible",
    }
)
# providers OmniRoute translates from a chat API of their own (one prompt string, an app id), not a base URL and a key
PROPRIETARY = frozenset({"oneminai", "free-ai", "dify"})
NOT_CHAT_SETS = frozenset({"enterprise", "imageOnly", "video"})
NOT_USABLE_FLAGS = ("deprecated", "hiddenFromDashboard", "subscriptionRisk", "noAuth", "browserBacked", "isLocalCli")
ENDPOINT_TAIL = re.compile(r"/(?:chat/completions|messages|responses)/?(?:\?.*)?$")
HINT_LIMIT = 240
NEUTRAL = "#64748B"  # the colour of a provider OmniRoute gives none
# the group of a provider by the family of the OmniRoute file it is in; the rest see group_of
GROUP_OF_FAMILY = {"local": "local", "gateways": "aggregator", "beatapi": "aggregator", "inference": "inference"}
GROUPS = ("login", "apikey", "free", "aggregator", "inference", "regional", "local", "custom")


def _extra(id: str, name: str, group: str, color: str, text_icon: str, api_format: str, hint: str, via: str) -> dict:
    return {
        "id": id,
        "alias": id,
        "name": name,
        "group": group,
        "color": color,
        "text_icon": text_icon,
        "website": "https://claude.ai" if id == "claude" else "",
        "key_hint": hint,
        "base_url": "",
        "api_format": api_format,
        "default_model": "",
        "key_required": False,
        "free": False,
        "via": via,
    }


EXTRA = (
    _extra(
        "claude",
        "Claude subscription",
        "login",
        "#D97757",
        "CL",
        "anthropic",
        "Sign in with a Claude account: a setup-token or OAuth, not a key. It does not go through the gateway.",
        "login",
    ),
    _extra(
        "custom-openai",
        "Custom OpenAI-compatible",
        "custom",
        "#64748B",
        "OA",
        "openai",
        "Any endpoint that speaks /chat/completions: give its base URL and key.",
        "gateway",
    ),
    _extra(
        "custom-anthropic",
        "Custom Anthropic-compatible",
        "custom",
        "#78716C",
        "AN",
        "anthropic",
        "Any endpoint that speaks /messages: give its base URL and key.",
        "gateway",
    ),
)


def api_root(url: str, api_format: str) -> str:
    """The root of an API (``https://x/v1``) from the URL of its chat endpoint (``https://x/v1/chat/completions``)."""
    root = ENDPOINT_TAIL.sub("", url.strip())
    if api_format == "gemini":
        root = re.sub(r"/models/?$", "", root)
    return root.rstrip("/")


def hint(display: dict) -> str:
    """How to get a key: OmniRoute's authHint, else its apiHint, else a pointer to the provider's website."""
    text = " ".join((display.get("authHint") or display.get("apiHint") or "").split())
    if not text and display.get("website"):
        return f"Create an API key on {display['website']}."
    if len(text) <= HINT_LIMIT:
        return text
    cut = text[:HINT_LIMIT]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return cut[: stop + 1] if stop > 60 else cut.rstrip() + "..."


def text_icon(shown: dict, id: str) -> str:
    """OmniRoute's textIcon, else the first two letters or digits of the name."""
    return shown.get("textIcon") or "".join(c for c in (shown.get("name") or id) if c.isalnum())[:2].upper()


def group_of(row: dict) -> str:
    """local, aggregator (the gateways family or OmniRoute's aggregator list), inference, regional, free (a free tier
    with a key), else apikey, the first that holds."""
    family = row["family"]
    if family in GROUP_OF_FAMILY:
        return GROUP_OF_FAMILY[family]
    if "aggregator" in row["sets"]:
        return "aggregator"
    if family == "regional":
        return "regional"
    return "free" if row["display"].get("hasFree") else "apikey"


def usable(row: dict) -> bool:
    """A chat LLM reached by a base URL and an API key, the gateway's way: not OAuth, cookie, no-auth, search, audio,
    image, video, cloud agent or system."""
    shown, entry = row["display"], row["registry"] or {}
    if "llm" not in (shown.get("serviceKinds") or []) or NOT_CHAT_SETS & set(row["sets"]):
        return False
    if row["id"] in PROPRIETARY or any(shown.get(flag) for flag in NOT_USABLE_FLAGS):
        return False
    if entry.get("executor") in BAD_EXECUTORS:
        return False
    if row["family"] == "local":
        return bool(shown.get("localDefault"))
    url = entry.get("baseUrl") or ""
    if entry.get("format") not in API_FORMATS or not url.startswith("http") or "{" in url or "0000000" in url:
        return False
    return entry.get("authType") in ("apikey", "optional")


def entry_of(row: dict) -> dict:
    shown, entry = row["display"], row["registry"] or {}
    local = row["family"] == "local"
    api_format = API_FORMATS[entry.get("format", "openai")]
    base = shown["localDefault"] if local else api_root(entry["baseUrl"], api_format)
    return {
        "id": row["id"],
        "alias": entry.get("alias") or shown.get("alias") or row["id"],
        "name": shown.get("name") or row["id"],
        "group": group_of(row),
        "color": shown.get("color") or NEUTRAL,
        "text_icon": text_icon(shown, row["id"]),
        "website": shown.get("website") or shown.get("apiKeyUrl") or "",
        "key_hint": hint(shown),
        "base_url": base,
        "api_format": api_format,
        "default_model": entry.get("defaultModel", ""),
        "key_required": not local and entry.get("authType") == "apikey",
        "free": bool(shown.get("hasFree")),
        "via": "gateway",
    }


def build(rows: list[dict], source: dict) -> dict:
    """The catalog from the rows the bundle printed: the usable providers sorted by name, after the extra entries."""
    found = sorted((entry_of(row) for row in rows if usable(row)), key=lambda e: (e["name"].lower(), e["id"]))
    ids = [e["id"] for e in found]
    if len(set(ids)) != len(ids):
        raise SystemExit("two providers of the same id: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    return {"source": source, "groups": list(GROUPS), "providers": [*EXTRA, *found]}


def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> str:
    done = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f"{' '.join(cmd)} failed:\n{done.stderr[-2000:]}")
    return done.stdout


def read_rows(checkout: Path, scratch: Path) -> list[dict]:
    run("npm", "install", "--silent", "--prefix", str(scratch), ESBUILD, ZOD)
    entry = scratch / "extract.ts"
    entry.write_text((HERE / "omniroute" / "extract.ts").read_text().replace("ROOT", str(checkout)))
    bundle = scratch / "extract.mjs"
    esbuild = scratch / "node_modules" / ".bin" / "esbuild"
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "NODE_PATH": str(scratch / "node_modules")}
    run(
        str(esbuild), str(entry), "--bundle", "--platform=node", "--format=esm", f"--outfile={bundle}",
        f"--tsconfig={checkout / 'tsconfig.json'}", "--log-level=warning", cwd=checkout, env=env,
    )  # fmt: skip
    return json.loads(run("node", str(bundle), cwd=checkout, env=env))


def describe(checkout: Path, tag: str) -> dict:
    commit = run("git", "rev-parse", "HEAD", cwd=checkout).strip()
    licence = (checkout / "LICENSE").read_text().splitlines()
    return {
        "name": "OmniRoute",
        "repository": REPO.removesuffix(".git"),
        "tag": tag,
        "version": tag.removeprefix("v"),
        "commit": commit,
        "license": "MIT",
        "copyright": next((line for line in licence if line.startswith("Copyright")), ""),
        "note": "Generated by scripts/generate_model_catalog.py from the registry and the display constants of "
        "OmniRoute; do not edit by hand.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="a tag of OmniRoute, such as v3.8.52")
    parser.add_argument("--source", type=Path, help="a checkout of that tag, instead of cloning it")
    parser.add_argument("--output", type=Path, default=OUT)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        checkout = args.source
        if checkout is None:
            checkout = scratch / "omniroute"
            run("git", "clone", "--quiet", "--depth", "1", "--branch", args.tag, REPO, str(checkout))
        catalog = build(read_rows(checkout.resolve(), scratch), describe(checkout, args.tag))
    args.output.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    print(f"{len(catalog['providers'])} providers of OmniRoute {args.tag} written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
