"""Regenerate the logos of the providers of the model catalog (web/public/provider-logos) and their manifest.

    uv run --with pillow python scripts/generate_provider_logos.py

Each provider of evo_agents/hub/model_catalog.json is mapped by LOGOS below to one source, in this order of preference:
Lobe Icons (``lobe:<name>``, the npm package @lobehub/icons-static-svg, MIT; the ``-color`` variant when there is
one), OmniRoute's own provider SVGs at the catalog's commit (``omniroute:<name>``, MIT, provenance in its
THIRD_PARTY_NOTICES.md), then an icon the provider publishes itself (an https URL: its favicon or app icon, a GitHub
organisation's avatar, or Google's cached copy of the favicon when the site turns robots away). SVGs are copied as
they are; any other image becomes a 64 px square PNG. Several providers on one source share one file.

Writes web/public/provider-logos/<file>, web/public/provider-logos/NOTICE.md (the source of every file) and
web/src/components/accounts/provider-logos.json ({provider id: file}), which provider-mark.tsx reads. A provider of
the catalog missing from LOGOS fails tests/hub/test_model_catalog.py: after a new catalog, map the new ids here (None
when the provider publishes no icon) and run this again. The logos are the marks of their owners, shown only to name
the provider beside its name.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CATALOG = ROOT / "evo_agents" / "hub" / "model_catalog.json"
OUT = ROOT / "web" / "public" / "provider-logos"
MANIFEST = ROOT / "web" / "src" / "components" / "accounts" / "provider-logos.json"

LOBE_VERSION = "1.100.0"
LOBE = f"https://registry.npmjs.org/@lobehub/icons-static-svg/-/icons-static-svg-{LOBE_VERSION}.tgz"
OMNIROUTE = "https://raw.githubusercontent.com/cheaper-inference/OmniRoute/{commit}/public/providers/{name}.svg"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36"
SIZE = 64  # px of a raster logo, twice the largest tile it is drawn in
MAX_SVG = 64 * 1024


def google(domain: str) -> str:
    """Google's cached copy of a site's favicon, for a site that answers robots with a challenge page."""
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=128"


# provider id -> source; None: the provider publishes no icon (its site is down or has none), so it keeps its tile
LOGOS: dict[str, str | None] = {
    "claude": "lobe:claude",
    "custom-openai": None,
    "custom-anthropic": None,
    "agentrouter": "https://agentrouter.org/logo.png",
    "agnes-cn": "lobe:agnesai",
    "aimlapi": "lobe:aimlapi",
    "ai21": "lobe:ai21",
    "ainative": "https://ainative.studio/icon-192x192.png",
    "aion": "lobe:aionlabs",
    "alibaba": "lobe:alibabacloud",
    "bailian-coding-plan": "lobe:bailian",
    "ant-ling": "lobe:ling",
    "anthropic": "lobe:anthropic",
    "anyapi": "https://cdn.prod.website-files.com/68512c600b9f5b4525766f6e/699d9d5280721f3a70f8f372_Logo256.png",
    "api-airforce": "https://api.airforce/airforce-logo.png",
    "apmix": "https://apmix.ai/icon.png",
    "arcee-ai": "lobe:arcee",
    "auriko": "https://www.auriko.ai/apple-icon.png?v=2",
    "bai": "https://b.ai/favicon.ico",
    "baichuan": "lobe:baichuan",
    "baidu": "lobe:baidu",
    "qianfan": "lobe:baiducloud",
    "baseten": "lobe:baseten",
    "bazaarlink": "https://bazaarlink.ai/favicon.svg",
    "beatapi": "https://beatapi.io/apple-touch-icon.png",
    "bigmodel": "lobe:zhipu",
    "bluesminds": "omniroute:bluesminds",
    "byteplus": "https://sf-bpcms.bytepluscdn.com/obj/byteplus-public-aiso/portal/assets/favicon.png",
    "bytez": "omniroute:bytez",
    "cerebras": "lobe:cerebras",
    "charm-hyper": "omniroute:charm-hyper",
    "chat-oripe": "https://oriper.com/logo/apple-icon-180x180.png",
    "chatanywhere": "https://chatanywhere.tech/brand/logo-icon.svg",
    "cheaperinference": "lobe:cheaperinference",
    "chenzk": "https://chenzk.top/favicon.ico",
    "chutes": "lobe:chutes",
    "cloudcode-one": "https://cloudcode.one/favicon.svg",
    "codestral": "lobe:mistral",
    "cohere": "lobe:cohere",
    "coze": "lobe:coze",
    "crof": "omniroute:crof",
    "daoxe": "https://daoxe.com/logo.png",
    "deepinfra": "lobe:deepinfra",
    "deepseek": "lobe:deepseek",
    "demonroute": "https://demonroute.com/nf/favicon-192.png",
    "dgrid": "omniroute:dgrid",
    "digitalocean": "lobe:digitalocean",
    "dit": "omniroute:dit",
    "docker-model-runner": "https://www.docker.com/favicon.ico",
    "doubao": "lobe:doubao",
    "dxnt": "https://www.dxnt.com/icon.svg",
    "electronhub": "https://www.electronhub.ai/images/electron.png",
    "eurouter": "https://www.eurouter.ai/favicon.ico",
    "factory": "omniroute:factory",
    "fastrouter": "https://fastrouter.ai/favicon.ico",
    "featherless-ai": "lobe:featherless",
    "fireworks": "lobe:fireworks",
    "freeaiapikey": "omniroute:freeaiapikey",
    "freeinference": None,
    "freemodel-dev": "omniroute:freemodel-dev",
    "freetheai": "https://freetheai.org/static/assets/FtaFavi-180.png",
    "friendliai": "lobe:friendli",
    "g4f-gemini": "https://g4f.dev/dist/img/g4f.svg",
    "g4f-groq": "https://g4f.dev/dist/img/g4f.svg",
    "g4f-nvidia": "https://g4f.dev/dist/img/g4f.svg",
    "g4f-ollama": "https://g4f.dev/dist/img/g4f.svg",
    "g4f-pollinations": "https://g4f.dev/dist/img/g4f.svg",
    "gemini": "lobe:gemini",
    "gigachat": "https://giga.chat/icons/gigachat/apple-icon-180x180.png",
    "gitlawb-gmi": "omniroute:gitlawb-gmi",
    "gitlawb": "omniroute:gitlawb",
    "glm": "lobe:zai",
    "glm-cn": "lobe:zhipu",
    "glmt": "lobe:zhipu",
    "greenpt": "lobe:greenpt",
    "groq": "lobe:groq",
    "helixmind": None,
    "helyxai": "https://helyxai.space/logo.jpeg",
    "hcnsec": "omniroute:hcnsec",
    "huggingface": "lobe:huggingface",
    "hyperbolic": "lobe:hyperbolic",
    "iflytek": "lobe:spark",
    "inception": "lobe:inception",
    "inference-net": "lobe:inference",
    "internlm": "lobe:internlm",
    "kenari": "omniroute:kenari",
    "kilo-gateway": "lobe:kilocode",
    "moonshot": "lobe:kimi",
    "lambda-ai": "lobe:lambda",
    "lemonade": "https://lemonade-server.ai/favicon.ico",
    "liquid": "lobe:liquid",
    "literouter": google("literouter.com"),
    "llama-cpp": "https://github.com/ggml-org.png",
    "llamafile": "https://github.com/Mozilla-Ocho.png",
    "llamagate": None,
    "llmgateway": "lobe:llmgateway",
    "llm-kiwi": google("llm.kiwi"),
    "llm7": "omniroute:llm7",
    "lm-studio": "lobe:lmstudio",
    "logfare": "https://logfare.ai/static/img/favicon.png",
    "longcat": "lobe:longcat",
    "lyceum": "https://lyceum.technology/apple-touch-icon.png",
    "maritalk": "https://www.maritaca.ai/favicon.svg",
    "meganova-ai": "https://www.meganova.ai/favicon.ico",
    "meta-llama": "lobe:meta",
    "minimax-cn": "lobe:minimax",
    "mistral": "lobe:mistral",
    "mixlayer": "https://www.mixlayer.com/icon.svg",
    "mlx-gemma": "https://github.com/ml-explore.png",
    "mlx-qwen": "https://github.com/ml-explore.png",
    "mnn-ai": google("mnnai.ru"),
    "modelscope": "lobe:modelscope",
    "morph": "lobe:morph",
    "naga-ai": "https://naga.ac/apple-icon.png",
    "naga-ac": "https://naga.ac/apple-icon.png",
    "nan": "https://nan.builders/apple-touch-icon.png",
    "nanogpt": "https://nano-gpt.com/favicon/android-chrome-192x192.png",
    "nara": "https://bynara.id/icon.svg",
    "navy": google("api.navy"),
    "nebius": "lobe:nebius",
    "nlpcloud": "lobe:nplcloud",
    "nous-research": "lobe:nousresearch",
    "nscale": "https://cdn.prod.website-files.com/69e759200831878be71184d8/"
    "6a7acbc1fde3243b269592af_66851aef2492ff79aa3bd859_favicon-32.png",
    "nube": "omniroute:nube",
    "nvidia": "lobe:nvidia",
    "triton": "lobe:nvidia",
    "ofoxai": "https://ofox.ai/icon.svg",
    "ollama-local": "lobe:ollama",
    "ollama-cloud": "lobe:ollama",
    "onomeo": "https://onomeo.com/icon.svg",
    "oobabooga": "https://github.com/oobabooga.png",
    "openadapter": "omniroute:openadapter",
    "openai": "lobe:openai",
    "opencode-go": "lobe:opencode",
    "opencode-zen": "lobe:opencode",
    "openference-api": "https://openference.com/brand/favicon.svg",
    "openrouter": "lobe:openrouter",
    "openvecta": "https://openvecta.com/logo.png",
    "opper": "omniroute:opper",
    "orcarouter": "omniroute:orcarouter",
    "ovhcloud": "https://www.ovhcloud.com/apple-touch-icon.png",
    "perplexity": "lobe:perplexity",
    "perplexity-agent": "lobe:perplexity",
    "pioneer": "omniroute:pioneer",
    "plamo": "https://plamo.preferredai.jp/plm_b_full_rgb.png",
    "poe": "lobe:poe",
    "poixe-ai": "https://poixe.com/x.svg",
    "poolside": "lobe:poolside",
    "publicai": "omniroute:publicai",
    "qiniu": "lobe:qiniu",
    "qwen-cloud": "lobe:qwen",
    "qwen-cloud-token-plan": "lobe:qwen",
    "regolo": "https://regolo.ai/wp-content/uploads/2025/11/regolo-icon.png",
    "reka": "lobe:reka",
    "requesty": "omniroute:requesty",
    "routeway": "https://routeway.ai/favicon.ico",
    "sambanova": "lobe:sambanova",
    "sarvam": "https://www.sarvam.ai/favicon.ico",
    "scaleway": "https://www.scaleway.com/favicon/website/apple-touch-icon-180x180.png",
    "sealion": google("sea-lion.ai"),
    "seekai": "https://seekai.cc/logo.png",
    "sensenova": "lobe:sensenova",
    "siliconflow": "lobe:siliconcloud",
    "sparkdesk": "lobe:spark",
    "speka": None,
    "stepfun": "lobe:stepfun",
    "sumopod": "omniroute:sumopod",
    "synthetic": "https://synthetic.new/favicon.svg",
    "tabitoken": google("tabitoken.com"),
    "tencent": "lobe:hunyuan",
    "token-kiosk": google("gaib.ai"),
    "tokenmarket": "https://www.tokensmarket.ai/logo-mark.png",
    "tokenreply": "https://cdn.tokenreply.com/239b521ab88e38bef121d065b64c5461ffc38493/logo.png",
    "tokenrouter": "omniroute:tokenrouter",
    "typhoon": "https://opentyphoon.ai/images/favicon.ico",
    "uc-direct": "https://uncensored.com/apple-icon.png",
    "unifically": "https://unifically.com/static/brand/logo-192.png",
    "unorouter": "https://unorouter.com/images/icons/icon-192.png",
    "upstage": "lobe:upstage",
    "v0-vercel": "lobe:v0",
    "venice": "lobe:venice",
    "vercel-ai-gateway": "lobe:vercel",
    "vllm": "lobe:vllm",
    "void-ai": google("voidai.app"),
    "volcengine": "lobe:volcengine",
    "volcengine-agent-plan": "lobe:volcengine",
    "volcengine-coding-plan": "lobe:volcengine",
    "wafer": "lobe:wafer",
    "wandb": "lobe:wandb",
    "writer": google("writer.com"),
    "x5lab": "omniroute:x5lab",
    "xiaomi-mimo": "https://mimo.mi.com/favicon.png",
    "xiaomi-mimo-token-plan": "https://mimo.mi.com/favicon.png",
    "xinference": "lobe:xinference",
    "xkiro": "https://xkiro.com/images/logo/logo-xt-green.png",
    "y-api": "https://y-api.bestvirtualgoods.com/favicon.svg",
    "yi": "lobe:yi",
    "yolo-auto": "https://yolo-auto.com/assets/yolo-auto-favicon.svg",
    "zai": "lobe:zai",
    "zenmux": "lobe:zenmux",
    "zerolimitai": "https://www.zerolimitai.com/apple-touch-icon.png",
    "zylo-api": "https://zyloai.net/assets/images/logo.png",
}


def fetch(url: str) -> bytes:
    # curl rather than urllib: some of these sites send an incomplete certificate chain that curl completes
    done = subprocess.run(["curl", "-fsSL", "--max-time", "30", "-A", UA, url], capture_output=True)
    if done.returncode:
        raise SystemExit(f"{url}: curl failed ({done.stderr.decode(errors='replace').strip()})")
    return done.stdout


def lobe_icons() -> dict[str, bytes]:
    """The SVGs of the pinned Lobe Icons package, by file name without .svg."""
    with tarfile.open(fileobj=io.BytesIO(fetch(LOBE)), mode="r:gz") as archive:
        return {
            Path(member.name).stem: archive.extractfile(member).read()
            for member in archive.getmembers()
            if member.isfile() and member.name.startswith("package/icons/") and member.name.endswith(".svg")
        }


def lobe_name(name: str, icons: dict[str, bytes]) -> str:
    """The colour variant of a Lobe icon when the package has one of a sensible size, else the icon itself."""
    for file in (f"{name}-color", name):
        if file in icons and len(icons[file]) <= MAX_SVG:
            return file
    raise SystemExit(f"Lobe Icons {LOBE_VERSION} has no icon {name!r} under {MAX_SVG} bytes")


def is_svg(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower())


def check_svg(source: str, data: bytes) -> bytes:
    text = data.decode("utf-8")
    if len(data) > MAX_SVG:
        raise SystemExit(f"{source}: an SVG of {len(data)} bytes, over {MAX_SVG}: map a raster icon instead")
    if "prefers-color-scheme" in text:
        raise SystemExit(f"{source}: the SVG changes with the colour scheme and would vanish on the white tile")
    root = re.search(r"<svg\b[^>]*>", text, re.S)
    if root is None:
        raise SystemExit(f"{source}: no <svg> element")
    if "viewBox=" not in root.group(0):
        # an SVG with a width and a height but no viewBox would not scale: give it the box of its own size
        size = [re.search(rf'\b{side}=["\']([0-9.]+)(?:px)?["\']', root.group(0)) for side in ("width", "height")]
        if not all(size):
            raise SystemExit(f"{source}: the SVG has neither a viewBox nor a width and a height")
        box = f' viewBox="0 0 {size[0].group(1)} {size[1].group(1)}"'
        text = text[: root.start()] + root.group(0)[:4] + box + root.group(0)[4:] + text[root.end() :]
    return text.encode("utf-8")


def to_png(source: str, data: bytes) -> bytes:
    from PIL import Image  # the one dependency, outside the project's: uv run --with pillow

    try:
        image = Image.open(io.BytesIO(data))
        if image.format == "ICO":
            image.size = max(image.ico.sizes())
        image = image.convert("RGBA")
    except Exception as error:  # noqa: BLE001 - any unreadable image stops the run with its source
        raise SystemExit(f"{source}: not an image ({error})") from error
    box = image.getbbox()
    if box is None:
        raise SystemExit(f"{source}: the image is empty")
    image = trim_white(image.crop(box))
    scale = SIZE / max(image.size)  # up as well as down: a 32 px favicon fills the square like a large icon
    image = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS
    )
    square = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    square.paste(image, ((SIZE - image.width) // 2, (SIZE - image.height) // 2))
    out = io.BytesIO()
    square.save(out, format="PNG", optimize=True)
    return out.getvalue()


def trim_white(image):
    """The image without a white margin round it, so the mark fills the tile; a coloured background is kept."""
    from PIL import Image, ImageChops

    corners = {image.getpixel(xy) for xy in ((0, 0), (image.width - 1, 0), (0, image.height - 1))}
    if not all(alpha == 255 and min(rgb) >= 240 for *rgb, alpha in corners):
        return image
    white = Image.new("RGBA", image.size, (255, 255, 255, 255))
    ink = ImageChops.difference(image, white).convert("L").point(lambda value: 255 if value > 24 else 0)
    box = ink.getbbox()
    return image.crop(box) if box else image


def plan(catalog_ids: list[str], logos: dict[str, str | None]) -> dict[str, tuple[str, str]]:
    """{provider id: (source, file)}: providers on one source share the file named after the first of them.
    Fails on a catalog provider LOGOS does not map and on an entry of LOGOS the catalog does not hold."""
    missing = [id for id in catalog_ids if id not in logos]
    stale = sorted(set(logos) - set(catalog_ids))
    if missing or stale:
        raise SystemExit(f"LOGOS does not match the catalog: unmapped {missing}, not in the catalog {stale}")
    files: dict[str, str] = {}
    out: dict[str, tuple[str, str]] = {}
    for id in catalog_ids:
        source = logos[id]
        if source is not None:
            out[id] = (source, files.setdefault(source, id))
    return out


def render(source: str, icons: dict[str, bytes], commit: str) -> tuple[bytes, str, str]:
    """(bytes, extension, where it came from) of one source."""
    kind, _, name = source.partition(":")
    if kind == "lobe":
        file = lobe_name(name, icons)
        return check_svg(source, icons[file]), "svg", f"Lobe Icons {LOBE_VERSION}, icons/{file}.svg (MIT)"
    if kind == "omniroute":
        url = OMNIROUTE.format(commit=commit, name=name)
        return check_svg(source, fetch(url)), "svg", f"OmniRoute {commit[:8]}, public/providers/{name}.svg (MIT)"
    data = fetch(source)
    if is_svg(data):
        return check_svg(source, data), "svg", source
    return to_png(source, data), "png", source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=OUT)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    args = parser.parse_args(argv)
    catalog = json.loads(CATALOG.read_text())
    commit = catalog["source"]["commit"]
    planned = plan([provider["id"] for provider in catalog["providers"]], LOGOS)
    icons = lobe_icons()
    args.output.mkdir(parents=True, exist_ok=True)
    for old in args.output.iterdir():
        if old.suffix in {".svg", ".png"}:
            old.unlink()
    written: dict[str, tuple[str, str]] = {}  # source -> (file name, where from)
    manifest: dict[str, str] = {}
    failed: list[str] = []
    for id, (source, stem) in planned.items():
        if source not in written:
            try:
                data, extension, origin = render(source, icons, commit)
            except SystemExit as error:
                failed.append(f"{id}: {error}")
                continue
            name = f"{stem}.{extension}"
            (args.output / name).write_bytes(data)
            written[source] = (name, origin)
        if source in written:
            manifest[id] = written[source][0]
    if failed:
        print("\n".join(["no logo written for:", *failed]), file=sys.stderr)
        return 1
    args.manifest.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    rows = "\n".join(f"| `{name}` | {origin} |" for name, origin in sorted(written.values()))
    (args.output / "NOTICE.md").write_text(
        "# Provider logos\n\n"
        "Generated by `scripts/generate_provider_logos.py`; do not edit by hand. Each logo is the mark of its owner,\n"
        "shown only to name a provider of the model catalog beside its name. Lobe Icons: Copyright (c) 2023 LobeHub,\n"
        "MIT. OmniRoute: Copyright (c) 2026 diegosouzapw, MIT (its THIRD_PARTY_NOTICES.md names where each SVG comes\n"
        "from). Any other file is the icon the provider publishes on the address given, as a 64 px PNG unless it is\n"
        "an SVG.\n\n"
        "| File | Source |\n| --- | --- |\n" + rows + "\n"
    )
    unmapped = [id for id, source in LOGOS.items() if source is None]
    print(f"{len(manifest)} providers on {len(written)} logos written to {args.output}; no logo: {', '.join(unmapped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
