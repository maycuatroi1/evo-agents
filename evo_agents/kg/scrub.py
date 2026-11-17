"""Secrets in text: what the hub's scrub (``evo_agents.hub.server.kg_scrub``) reads source blobs, run logs and the
rows of a graph with. It never returns, prints or logs a value: a finding counts the secrets of each kind.

``secrets_in`` and ``blob_secrets`` read text with the git connector's own patterns (``evo_agents.kg.redact``), a blob
as the file the items of the run logs say it is (``run_summary``): its path decides whether a bare value after ``=``
is an expression or a literal. ``might_hold_secret`` tells text that holds none cheaply. ``row_secrets`` reads a row
of a graph, a text and a JSON document; ``evo_agents.hub.server.kg_scrub.graph_secrets`` reads a graph file with it.

Old copies of secrets are found and deleted on the hub only (``evo-agents hub kg scrub``, or ``kg scrub`` for the
project of the current directory): no kg home on a machine holds a corpus, a memo or a graph any more.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.corpus import Corpus
from evo_agents.kg.redact import redact_file
from evo_agents.secret_patterns import PROVIDER_KEYS

BLOB_REF = re.compile(r"sha256:([0-9a-f]{64})")
MIN_STRING = 8  # a string shorter than this holds no secret the patterns take
# What any text the patterns of evo_agents.kg.redact take must hold (``might_hold_secret``): in lower case, the end of
# a secret's name before its assignment or a PEM header; or a URI with a password; or the prefix of a provider's key.
# Text without any of it is not read further, which makes a scan several times faster. A pattern added there must add
# its condition here: tests/kg/test_scrub.py checks every case of tests/kg/test_redact.py.
NAME_END = re.compile(r"(?:word|wd|phrase|secret|[iset_-]key|token)[\"']?[ \t]*[=:]")  # on the lower-cased text
URI_WITH_PASSWORD = re.compile(r"://[^:/@\s]*:[^@\s/]+@")
# A part of each provider's prefix (evo_agents.secret_patterns.PROVIDER_KEYS): a fine-grained GitHub token holds _pat_.
KEY_PREFIXES = ("sk-", "AKIA", "ASIA", "ABIA", "ACCA", "A3T", "xox", "AIza", "_pat_")
KEY_PREFIXES += tuple(f"gh{kind}_" for kind in "opsur") + tuple(f"ev{kind}_" for kind in "hsw")

# Reading text


def might_hold_secret(text: str) -> bool:
    """Whether ``text`` holds what every secret the patterns take holds (see NAME_END); False means it holds none."""
    low = text.lower()
    if "private key" in low or NAME_END.search(low):
        return True
    if "://" in text and URI_WITH_PASSWORD.search(text):
        return True
    if not any(prefix in text for prefix in KEY_PREFIXES):
        return False
    return any(pattern.search(text) for _, pattern in PROVIDER_KEYS)


def secrets_in(text: str, path: str = "") -> dict[str, int]:
    """The secrets the git connector would redact from ``text``, read as the file at ``path``: kind -> count."""
    if not might_hold_secret(text):
        return {}
    return redact_file(text, path)[1]


def blob_secrets(text: str, paths: Iterable[str] = ()) -> dict[str, int]:
    """The secrets of a blob's ``text``, read as the first of ``paths`` (the files the items referring to it are)
    where it holds any; read as a document when no path is known."""
    if not might_hold_secret(text):
        return {}
    for path in sorted(name for name in paths if name) or [""]:
        found = redact_file(text, path)[1]
        if found:
            return found
    return {}


def _strings(value) -> Iterator[str]:
    if isinstance(value, str):
        if len(value) >= MIN_STRING:
            yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _most(*found: dict[str, int]) -> dict[str, int]:
    """Per kind, the highest count of ``found``: two readings of one text count a secret once."""
    most: dict[str, int] = {}
    for counts in found:
        for kind, count in counts.items():
            most[kind] = max(most.get(kind, 0), count)
    return dict(sorted(most.items()))


def secrets_in_json(text: str) -> dict[str, int]:
    """The secrets of a JSON document: in its text, where a key names its value, and in each string it holds, which
    may be a file's text with its own lines."""
    try:
        value = json.loads(text)
    except ValueError:
        return secrets_in(text)
    return _most(secrets_in(text), secrets_in("\n".join(_strings(value))))


def blob_refs(record: dict) -> list[str]:
    """The hex SHA-256 of every blob an item record of a run log refers to: its body and its fragments."""
    refs = []
    body = record.get("body")
    for part in [body] + list(record.get("fragments") or []):
        if isinstance(part, dict) and isinstance(part.get("blob"), str):
            found = BLOB_REF.fullmatch(part["blob"])
            if found:
                refs.append(found[1])
    return refs


def item_path(record: dict) -> str:
    """The path of the file an item record is, from its properties; empty when it names none."""
    props = record.get("props")
    path = props.get("path") if isinstance(props, dict) else None
    return path if isinstance(path, str) else ""


@dataclass
class RunSummary:
    """What one run log says about the blobs of its source."""

    run_id: str | None
    source: str | None
    refs: set[str]  # the blobs its items refer to
    full: bool  # a complete listing and an ok end: every item of the source as the run saw it, and no other
    paths: dict[str, set[str]] = field(default_factory=dict)  # blob -> the paths of the items referring to it


def run_summary(path: Path) -> RunSummary:
    """Read the run log at ``path`` (jsonl, or jsonl.gz) for its blobs, the paths its items give them, and whether
    the run was full."""
    summary = RunSummary(None, None, set(), False)
    listed = ended = False
    for record in Corpus.read_log(path):
        kind = record.get("type")
        if kind == "run":
            summary.run_id, summary.source = record.get("run_id"), record.get("source")
        elif kind == "item":
            refs = blob_refs(record)
            summary.refs.update(refs)
            name = item_path(record)
            for ref in refs if name else ():
                summary.paths.setdefault(ref, set()).add(name)
        elif kind == "listing":
            listed = listed or record.get("complete") is True
        elif kind == "run_end":
            ended = record.get("status") == "ok"
    summary.full = listed and ended
    return summary


def row_secrets(text: str | None, document: str | None) -> dict[str, int]:
    """The secrets of a row of a graph: a text and a JSON document, either empty."""
    return _most(secrets_in(text or ""), secrets_in_json(document) if document else {})
