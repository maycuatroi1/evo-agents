"""Data loss prevention of a project: the rules that find customer identifiers, confidential financial figures and
confidential-contract keywords in what a model call sends, and what a policy does with them (``docs/hub.md``, section
"Data loss prevention").

A project's policy (dlp_policies, 0264) has a mode, off, redact or block, the rule kinds of KINDS it applies, and
keywords of its own added to CONTRACT_KEYWORDS. ``apply`` reads a text, or every string of a JSON body (``apply_json``),
and gives what the policy makes of it: in redact mode each match replaced by ``[REDACTED:<kind>]``, in block mode the
same text with ``blocked`` set, and in both the number of matches of each kind. The hub's gateway route
(``evo_agents.hub.server.gateway.routes.proxy``) and the hub runtime (``evo_agents.hub.server.hub_runtime``) apply the policy of
the run's project before a call leaves for a provider (``evo_agents.hub.server.dlp_policy``): a call blocked never leaves.

A match's value never leaves this module: a ``Scan`` holds counts by kind, and the redacted text. Nothing here logs.

The kinds:

- ``cif``: a customer information file code, ``CIF`` followed by 6 to 12 digits.
- ``national_id``: a citizen identity card number (CCCD), 12 digits with a province code of 001 to 096 and a century
  digit, alone or after CCCD or CMND; or 9 digits after CMND.
- ``account_number``: a bank account number, 8 to 19 digits (spaces and hyphens between groups) after a word that names
  one: account, acct, a/c, STK, tai khoan.
- ``financial_figure``: an amount of money of at least four digits with a currency or a scale: VND, USD, EUR, dong,
  ty, trieu, billion, million, or a leading ``$``.
- ``contract_keyword``: a keyword of a confidential contract, CONTRACT_KEYWORDS and the policy's own, matched whole
  words, case and accents aside.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

MODES = ("off", "redact", "block")  # dlp_policies.mode (0264)
KINDS = ("cif", "national_id", "account_number", "financial_figure", "contract_keyword")  # dlp_policies.kinds (0264)
SURFACES = ("gateway_proxy", "hub_runtime")  # dlp_counts.surface (0264)
ACTIONS = ("redacted", "blocked")  # dlp_counts.action (0264)
MAX_KEYWORDS = 50  # dlp_policies.keywords (0264)
MAX_KEYWORD_CHARS = 100
MAX_INSPECT_BYTES = 256 * 1024  # the text POST /v1/projects/{p}/dlp/inspect reads

CONTRACT_KEYWORDS = (
    "confidential contract",
    "strictly confidential",
    "non-disclosure agreement",
    "hop dong mat",
    "tuyet mat",
    "bao mat noi bo",
)

DIGITS = r"(?<![\dA-Za-z])"  # no digit or letter just before a number
END = r"(?![\dA-Za-z])"  # nor just after it
PATTERNS: dict[str, re.Pattern] = {
    "cif": re.compile(rf"\bCIF[\s:#.-]*\d{{6,12}}{END}", re.IGNORECASE),
    "national_id": re.compile(
        rf"(?:\b(?:CCCD|CMND)[\s:#.-]*)?{DIGITS}0(?:0[1-9]|[1-8]\d|9[0-6])[0-9]\d{{8}}{END}"
        rf"|\bCMND[\s:#.-]*\d{{9}}{END}",
        re.IGNORECASE,
    ),
    "account_number": re.compile(
        rf"\b(?:account(?:\s+(?:no\.?|number))?|acct|a/c|STK|so\s+tai\s+khoan|tai\s+khoan)[\s:#.-]*"
        rf"\d(?:[\s-]?\d){{7,18}}{END}",
        re.IGNORECASE,
    ),
    "financial_figure": re.compile(
        r"\$\s?\d{1,3}(?:[,.]\d{3})+(?:[.,]\d+)?|\$\s?\d{4,}(?:[.,]\d+)?"
        rf"|{DIGITS}\d{{1,3}}(?:[,. ]\d{{3}})+(?:[.,]\d+)?\s?(?:VND|USD|EUR|dong|ty|trieu|billion|million)\b"
        rf"|{DIGITS}\d{{4,}}(?:[.,]\d+)?\s?(?:VND|USD|EUR|dong|ty|trieu|billion|million)\b",
        re.IGNORECASE,
    ),
}


def fold(text: str) -> str:
    """``text`` with its accents taken off (and đ as d), so a rule matches Vietnamese written either way; one
    character for each of ``text``'s, so a match's span is the same in both."""
    out = []
    for char in text:
        if char in "đĐ":
            out.append("d" if char == "đ" else "D")
            continue
        bare = unicodedata.normalize("NFD", char)[0]
        out.append(bare if len(bare) == 1 else char)
    return "".join(out)


@dataclass(frozen=True)
class Policy:
    mode: str = "off"
    kinds: tuple[str, ...] = KINDS
    keywords: tuple[str, ...] = ()

    @property
    def on(self) -> bool:
        return self.mode != "off" and bool(self.kinds)


@dataclass
class Scan:
    """What a policy made of a text: the text to send, the matches of each kind, and whether it is blocked."""

    text: str
    counts: Counter = field(default_factory=Counter)
    blocked: bool = False

    def add(self, other: Scan) -> None:
        self.counts.update(other.counts)
        self.blocked = self.blocked or other.blocked


def _keyword_pattern(policy: Policy) -> re.Pattern | None:
    words = sorted({fold(word).strip().lower() for word in (*CONTRACT_KEYWORDS, *policy.keywords) if word.strip()})
    if not words:
        return None
    alternatives = "|".join(r"\s+".join(map(re.escape, word.split())) for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w)", re.IGNORECASE)


def _spans(policy: Policy, folded: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for kind in KINDS:
        if kind not in policy.kinds:
            continue
        pattern = _keyword_pattern(policy) if kind == "contract_keyword" else PATTERNS[kind]
        if pattern is not None:
            found += [(m.start(), m.end(), kind) for m in pattern.finditer(folded)]
    found.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    kept: list[tuple[int, int, str]] = []
    for span in found:  # overlapping matches: the earliest, then the longest, wins
        if not kept or span[0] >= kept[-1][1]:
            kept.append(span)
    return kept


def apply(policy: Policy, text: str) -> Scan:
    """What ``policy`` makes of ``text`` (see the module)."""
    if not policy.on or not text:
        return Scan(text)
    spans = _spans(policy, fold(text))
    if not spans:
        return Scan(text)
    counts = Counter(kind for _, _, kind in spans)
    if policy.mode == "block":
        return Scan(text, counts, blocked=True)
    parts, at = [], 0
    for start, end, kind in spans:
        parts += [text[at:start], f"[REDACTED:{kind}]"]
        at = end
    parts.append(text[at:])
    return Scan("".join(parts), counts)


def _walk(policy: Policy, value, scan: Scan):
    if isinstance(value, str):
        found = apply(policy, value)
        scan.add(found)
        return found.text
    if isinstance(value, list):
        return [_walk(policy, item, scan) for item in value]
    if isinstance(value, dict):
        return {key: item if key in KEPT_KEYS else _walk(policy, item, scan) for key, item in value.items()}
    return value


KEPT_KEYS = frozenset({"model", "role", "type", "id", "tool_call_id", "name"})  # a call's routing, never content


def apply_json(policy: Policy, body):
    """(``body`` as ``policy`` makes it, the ``Scan`` of every string in it but its routing keys)."""
    scan = Scan("")
    if not policy.on:
        return body, scan
    return _walk(policy, body, scan), scan


def apply_bytes(policy: Policy, content: bytes) -> tuple[bytes, Scan]:
    """(the JSON body ``content`` as ``policy`` makes it, its ``Scan``); a body that is no JSON is read as text."""
    if not policy.on or not content:
        return content, Scan("")
    try:
        body = json.loads(content)
    except ValueError:
        found = apply(policy, content.decode("utf-8", "replace"))
        return found.text.encode(), found
    made, scan = apply_json(policy, body)
    if not scan.counts:
        return content, scan
    return json.dumps(made).encode(), scan
