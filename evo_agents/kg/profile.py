"""The data profile of a build: what the corpus behind a graph is made of, computed without a model.

Every build writes one into its coverage (``coverage["profile"]``), on a machine and on the hub alike: item, line and
character counts; nodes by kind; per code language, the files and the share of them with call and inheritance edges;
the share of code, prose, conversation, structured records and binary files; the spread of content lengths and the
passage count; the share of Vietnamese and English text; the shapes that repeat as identifiers (candidates for the
``identifiers`` of knowledge.yaml); how many strings look like an email, a phone number, a national id or a medical
code; the share of mentions that resolved; noise; the depth of commit history; and each source's label.

It holds counts and shapes, never a value (a shape is ``A{3}-9{4}``, not ``INV-2024``), so it reaches any reader of
the graph, and a Curator prompt, whatever the label of the items it describes. It sits in the coverage, which the
graph's content hash leaves out, so a profile changes neither a hash nor what any kg_* tool answers.

An item's own figures (``item_stats``) are memoized by the item's hash; ``build_profile`` adds them up.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import PurePosixPath

PROFILE_VERSION = "1"
SCAN_CHARS = 200_000  # an item's text is read for its shapes and signals up to here
TOP_SHAPES = 20
MIN_SHAPE_COUNT = 5  # a shape is a candidate identifier from this many occurrences in two items at least
SHORT = 20  # an item with fewer non-space characters than this is noise

CODE_KINDS = {"code", "markup"}
PROSE_KINDS = {"markdown", "text", "docx", "doc", "html", "wiki", "page", "pdf"}
CONVERSATION_KINDS = {"chat", "thread", "message", "conversation", "memory", "email"}
STRUCTURED_KINDS = {"config", "yaml", "manifest", "contracts", "plan", "ticket", "record", "table", "row", "json"}
HISTORY_KINDS = {"commit", "pr"}
BINARY_KINDS = {"asset", "binary", "image", "audio", "video"}
CLASSES = ("code", "prose", "conversation", "structured", "binary", "history", "other")

VI_LETTERS = frozenset("ăđơưạảãấầẩẫậắằẳẵặẹẻẽếềểễệịỉĩọỏốồổỗộớờởỡợụủứừửữựỵỷỹ")
EN_WORDS = frozenset(
    "the and of to in is that for it with as was on are this be by or at from have not but they which you were "
    "their has had will can".split()
)
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".dart": "dart",
    ".sql": "sql",
    ".sh": "shell",
    ".vue": "vue",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
TOKEN = re.compile(r"(?<![\w.])[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+){0,4}(?![\w])")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w.])\+?\d[\d ()./-]{7,17}\d(?![\w])")
NATIONAL_ID = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
MEDICAL_CODE = re.compile(r"\b[A-TV-Z][0-9][0-9AB]\.[0-9A-TV-Z]{1,4}\b")
DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")


def class_of(kind: str, declares: bool = False) -> str:
    """The class of an item kind; ``declares``: the item states nodes or edges itself (a record of a connector)."""
    if kind in CODE_KINDS:
        return "code"
    if kind in PROSE_KINDS:
        return "prose"
    if kind in CONVERSATION_KINDS:
        return "conversation"
    if kind in STRUCTURED_KINDS or declares:
        return "structured"
    if kind in BINARY_KINDS:
        return "binary"
    if kind in HISTORY_KINDS:
        return "history"
    return "other"


def language_of(record: dict) -> str | None:
    """The programming language of a code item: its file's suffix, else the language its body names."""
    path = (record.get("props") or {}).get("path") or ""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in LANGUAGE_BY_SUFFIX:
        return LANGUAGE_BY_SUFFIX[suffix]
    body = record.get("body") or {}
    found = body.get("language") if isinstance(body, dict) else None
    if isinstance(found, str) and found:
        return found.lower()
    return suffix.lstrip(".") or "unknown"


def natural_language(text: str) -> str | None:
    """``vi``, ``en`` or ``other`` for a text of prose; None when it has too few words to tell."""
    words = WORD.findall(text[:SCAN_CHARS])
    letters = sum(len(w) for w in words)
    if len(words) < 5 or letters == 0:
        return None
    lowered = text[:SCAN_CHARS].lower()
    if sum(1 for ch in lowered if ch in VI_LETTERS) / letters > 0.02:
        return "vi"
    if sum(1 for w in words if w.lower() in EN_WORDS) / len(words) >= 0.1:
        return "en"
    return "other"


def shape_of(token: str) -> str:
    """The shape of a token: runs of upper case ``A``, lower case ``a`` and digits ``9`` with their lengths, other
    characters as they are (``J45.909`` is ``A9{2}.9{3}``). It holds no letter or digit of the value."""
    out: list[str] = []
    prev, run = "", 0

    def flush() -> None:
        if prev:
            out.append(prev if run == 1 else f"{prev}{{{run}}}")

    for ch in token:
        cls = "A" if ch.isupper() else "a" if ch.islower() else "9" if ch.isdigit() else ch
        if cls == prev and cls in "Aa9":
            run += 1
            continue
        flush()
        prev, run = cls, 1
    flush()
    return "".join(out)


_SHAPE_PART = re.compile(r"([Aa9])(?:\{(\d+)\})?|(.)")


def shape_regex(shape: str) -> str:
    """A regular expression that matches the tokens of a shape, as an ``identifiers`` pattern may be written."""
    parts = []
    for m in _SHAPE_PART.finditer(shape):
        cls, n, other = m.groups()
        if other is not None:
            parts.append(re.escape(other))
        else:
            parts.append({"A": "[A-Z]", "a": "[a-z]", "9": r"\d"}[cls] + (f"{{{n}}}" if n else ""))
    return r"\b" + "".join(parts) + r"\b"


def _shapes(text: str) -> dict[str, int]:
    found: Counter[str] = Counter()
    for m in TOKEN.finditer(text):
        token = m.group(0)
        if not 3 <= len(token) <= 32 or not any(c.isdigit() for c in token) or not any(c.isalpha() for c in token):
            continue
        found[shape_of(token)] += 1
    return dict(found.most_common(TOP_SHAPES))


def _phones(text: str) -> int:
    count = 0
    for m in PHONE.finditer(text):
        found = m.group(0)
        if DATE.fullmatch(found):
            continue
        if 9 <= sum(c.isdigit() for c in found) <= 15:
            count += 1
    return count


def signals(text: str) -> dict[str, int]:
    """How many strings of ``text`` have the shape of an email, a phone number, a national id or a medical code."""
    return {
        "email": len(EMAIL.findall(text)),
        "phone": _phones(text),
        "national_id": len(NATIONAL_ID.findall(text)),
        "medical_code": len(MEDICAL_CODE.findall(text)),
    }


def item_stats(record: dict, text: str | None, declares: bool = False) -> dict:
    """One item's figures (JSON, memoizable): its class, characters and non-blank lines, language, shapes, signals."""
    kind = record.get("kind") or "other"
    cls = class_of(kind, declares)
    stats: dict = {"class": cls, "kind": kind, "chars": 0, "lines": 0, "tiny": False}
    if cls == "code":
        stats["language"] = language_of(record)
    if text is None:
        stats["tiny"] = cls not in ("binary", "history")
        return stats
    stats["chars"] = len(text)
    stats["lines"] = sum(1 for line in text.splitlines() if line.strip())
    stats["tiny"] = len("".join(text.split())) < SHORT
    head = text[:SCAN_CHARS]
    if cls in ("prose", "conversation", "structured"):
        language = natural_language(head)
        if language:
            stats["nl"] = language
    if cls != "code":
        stats["shapes"] = _shapes(head)
    found = {k: v for k, v in signals(head).items() if v}
    if found:
        stats["signals"] = found
    return stats


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _file_edges(graph) -> tuple[set[str], set[str]]:
    """The file nodes with a symbol that calls, and with one that inherits."""
    owner: dict[str, str] = {}
    for e in graph.edges.values():
        if e["rel"] == "declares":
            owner[e["dst"]] = e["src"]
    calls, inherits = set(), set()
    for e in graph.edges.values():
        file = owner.get(e["src"])
        if file is None:
            continue
        if e["rel"] == "calls":
            calls.add(file)
        elif e["rel"] == "inherits":
            inherits.add(file)
    return calls, inherits


def _history(graph) -> dict:
    times = sorted(
        t for n in graph.nodes.values() if n["kind"] == "Commit" for t in [(n.get("props") or {}).get("time")] if t
    )
    out = {"commits": sum(1 for n in graph.nodes.values() if n["kind"] == "Commit")}
    out["pull_requests"] = sum(1 for n in graph.nodes.values() if n["kind"] == "PullRequest")
    if times:
        out["first"], out["last"] = times[0], times[-1]
    return out


def _label_of(policy, source_id: str) -> dict:
    try:
        label = policy.source_label(source_id, None)
        return {"level": policy.level_name(label.level), "location": policy.location_name(label.location)}
    except Exception:  # a policy that cannot name its label leaves it out of the profile
        return {}


def build_profile(graph, rows: Iterable[tuple[str, str, dict]], coverage: dict, policy=None) -> dict:
    """The profile of a built ``graph``: ``rows`` are (source id, item id, ``item_stats``) of every item it read,
    ``coverage`` the build's coverage (``evo_agents.kg.build``), ``policy`` the project's policy for source labels."""
    rows = list(rows)
    calls, inherits = _file_edges(graph)
    totals = {"items": len(rows), "content_items": 0, "lines": 0, "chars": 0}
    classes: dict[str, dict] = {c: {"items": 0, "lines": 0, "chars": 0} for c in CLASSES}
    languages: dict[str, dict] = defaultdict(lambda: {"files": 0, "lines": 0, "calls": 0, "inherits": 0})
    sources: dict[str, dict] = defaultdict(lambda: {"items": 0, "lines": 0, "chars": 0})
    natural: Counter[str] = Counter()
    shapes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    found_signals: Counter[str] = Counter()
    signal_items: Counter[str] = Counter()
    lengths: list[int] = []
    tiny = binary = 0
    for source, item_id, s in rows:
        cls = s["class"]
        entry = classes.setdefault(cls, {"items": 0, "lines": 0, "chars": 0})
        entry["items"] += 1
        entry["lines"] += s["lines"]
        entry["chars"] += s["chars"]
        src = sources[source]
        src["items"] += 1
        src["lines"] += s["lines"]
        src["chars"] += s["chars"]
        if s["chars"]:
            totals["content_items"] += 1
            totals["lines"] += s["lines"]
            totals["chars"] += s["chars"]
            lengths.append(s["chars"])
        binary += cls == "binary"
        tiny += bool(s["tiny"]) and cls != "binary"
        if cls == "code":
            lang = languages[s.get("language") or "unknown"]
            lang["files"] += 1
            lang["lines"] += s["lines"]
            lang["calls"] += item_id in calls
            lang["inherits"] += item_id in inherits
        if s.get("nl"):
            natural[s["nl"]] += s["chars"]
        for shape, n in (s.get("shapes") or {}).items():
            shapes[shape][0] += n
            shapes[shape][1] += 1
        for k, n in (s.get("signals") or {}).items():
            found_signals[k] += n
            signal_items[k] += 1
    kinds = Counter(n["kind"] for n in graph.nodes.values())
    passage_counts = [len(p) for p in graph.passages.values()]
    mentions = {k: sum(int(e.get(k) or 0) for e in coverage.get("sources", [])) for k in ("mentions", "resolved")}
    mentions["dangling"] = sum(int(e.get("dangling") or 0) for e in coverage.get("sources", []))
    mentions["resolved_ratio"] = _ratio(mentions["resolved"], mentions["mentions"])
    candidates = [
        {"shape": shape, "pattern": shape_regex(shape), "count": n, "items": items}
        for shape, (n, items) in shapes.items()
        if n >= MIN_SHAPE_COUNT and items >= 2
    ]
    candidates.sort(key=lambda c: (-c["count"], c["shape"]))
    text_chars = sum(natural.values())
    return {
        "version": PROFILE_VERSION,
        **totals,
        "nodes": dict(sorted(kinds.items())),
        "edges": len(graph.edges),
        "classes": {
            c: {**v, "share": _ratio(v["chars"], totals["chars"]), "item_share": _ratio(v["items"], len(rows))}
            for c, v in classes.items()
            if v["items"]
        },
        "languages": {
            name: {
                **v,
                "calls_ratio": _ratio(v["calls"], v["files"]),
                "inherits_ratio": _ratio(v["inherits"], v["files"]),
            }
            for name, v in sorted(languages.items(), key=lambda kv: (-kv[1]["lines"], kv[0]))
        },
        "length": {
            "p50": _percentile(lengths, 0.5),
            "p90": _percentile(lengths, 0.9),
            "p99": _percentile(lengths, 0.99),
            "max": max(lengths, default=0),
            "passages": sum(passage_counts),
            "nodes_with_passages": len(passage_counts),
        },
        "natural_language": {
            "chars": dict(sorted(natural.items())),
            "share": {k: _ratio(v, text_chars) for k, v in sorted(natural.items())},
        },
        "identifier_candidates": candidates[:TOP_SHAPES],
        "sensitive": {
            k: {"strings": found_signals[k], "items": signal_items[k]}
            for k in ("email", "phone", "national_id", "medical_code")
        },
        "mentions": mentions,
        "noise": {
            "binary": binary,
            "tiny": tiny,
            "ratio": _ratio(binary + tiny, len(rows)),
        },
        "history": _history(graph),
        "sources": [
            {"id": sid, **v, **({"label": _label_of(policy, sid)} if policy is not None else {})}
            for sid, v in sorted(sources.items())
        ],
    }


def of_coverage(coverage: dict | None) -> dict | None:
    """The profile a build's coverage holds; None for a build made before profiles."""
    found = (coverage or {}).get("profile")
    return found if isinstance(found, dict) else None


def render(profile: dict) -> str:
    """The profile as text for ``kg profile``."""
    lines = [
        f"items {profile['items']} ({profile['content_items']} with content), {profile['lines']} lines, "
        f"{profile['chars']} chars; {sum(profile['nodes'].values())} nodes, {profile['edges']} edges"
    ]
    lines.append("nodes: " + ", ".join(f"{k} {v}" for k, v in sorted(profile["nodes"].items(), key=lambda kv: -kv[1])))
    lines.append(
        "classes: "
        + ", ".join(
            f"{c} {v['item_share']:.0%} of items, {v['share']:.0%} of chars" for c, v in profile["classes"].items()
        )
    )
    for name, v in profile["languages"].items():
        lines.append(
            f"  {name}: {v['files']} files, {v['lines']} lines, calls {v['calls_ratio']:.0%}, "
            f"inherits {v['inherits_ratio']:.0%}"
        )
    n = profile["length"]
    lines.append(
        f"length: p50 {n['p50']}, p90 {n['p90']}, p99 {n['p99']}, max {n['max']} chars; "
        f"{n['passages']} passages over {n['nodes_with_passages']} nodes"
    )
    share = profile["natural_language"]["share"]
    if share:
        lines.append("language: " + ", ".join(f"{k} {v:.0%}" for k, v in share.items()))
    if profile["identifier_candidates"]:
        lines.append(
            "identifier shapes: "
            + ", ".join(f"{c['shape']} x{c['count']}" for c in profile["identifier_candidates"][:8])
        )
    seen = {k: v for k, v in profile["sensitive"].items() if v["strings"]}
    lines.append(
        "sensitive: " + (", ".join(f"{k} {v['strings']} in {v['items']} items" for k, v in seen.items()) or "none")
    )
    m = profile["mentions"]
    lines.append(
        f"mentions: {m['resolved']}/{m['mentions']} resolved ({m['resolved_ratio']:.0%}), {m['dangling']} dangling"
    )
    lines.append(
        f"noise: {profile['noise']['ratio']:.0%} ({profile['noise']['binary']} binary, {profile['noise']['tiny']} tiny)"
    )
    h = profile["history"]
    lines.append(
        f"history: {h['commits']} commits, {h['pull_requests']} pull requests"
        + (f", {h['first']} to {h['last']}" if h.get("first") else "")
    )
    for s in profile["sources"]:
        label = s.get("label") or {}
        tag = f" [{label.get('level')}/{label.get('location')}]" if label else ""
        lines.append(f"  source {s['id']}{tag}: {s['items']} items, {s['lines']} lines")
    return "\n".join(lines)
