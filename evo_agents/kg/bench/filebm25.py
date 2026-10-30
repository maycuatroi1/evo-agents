"""Whole-file BM25: rank the files of a checkout by Okapi BM25 over their whole text, the way the public benches'
BM25 rows do, to tell what the node-level plain ranking costs (``kg bench run --search files``)."""

from __future__ import annotations

import math
import re
import subprocess
from collections import Counter
from pathlib import Path

from evo_agents.kg import search as text_search

K1 = 1.5
B = 0.75
MAX_BYTES = 1_000_000
_WORD = re.compile(r"[^\W_]+")
_CACHE: dict[tuple[str, str], Index] = {}


class Index:
    def __init__(self, texts: dict[str, str]):
        self.lengths: dict[str, int] = {}
        self.postings: dict[str, list[tuple[str, int]]] = {}
        for path, text in texts.items():
            counts = Counter(w.lower() for w in _WORD.findall(text))
            self.lengths[path] = sum(counts.values())
            for word, n in counts.items():
                self.postings.setdefault(word, []).append((path, n))
        self.average = (sum(self.lengths.values()) / len(self.lengths)) if self.lengths else 0.0

    def rank(self, query: str, limit: int) -> list[str]:
        words = dict.fromkeys(w.lower() for w in _WORD.findall(text_search.fold(query)))
        words = {w: 1 for w in words if w not in text_search.STOP_WORDS and (len(w) >= 2 or w.isdigit())}
        total = len(self.lengths)
        scores: dict[str, float] = {}
        for word in words:
            posts = self.postings.get(word, ())
            idf = math.log((total - len(posts) + 0.5) / (len(posts) + 0.5) + 1)
            for path, n in posts:
                norm = 1 - B + B * self.lengths[path] / (self.average or 1)
                scores[path] = scores.get(path, 0.0) + idf * n * (K1 + 1) / (n + K1 * norm)
        return sorted(scores, key=lambda p: (-scores[p], p))[:limit]


def read_files(clone: Path, commit: str, paths: set[str]) -> dict[str, str]:
    """The text of ``paths`` at ``commit`` (those that exist, are under MAX_BYTES and are not binary)."""
    listing = subprocess.run(
        ["git", "-C", str(clone), "ls-tree", "-r", "-z", "--long", commit], capture_output=True, check=True
    ).stdout.decode("utf-8", "replace")
    wanted = []
    for entry in listing.split("\0"):
        meta, _, path = entry.partition("\t")
        fields = meta.split()
        if len(fields) == 4 and path in paths and fields[3].isdigit() and int(fields[3]) <= MAX_BYTES:
            wanted.append((fields[2], path))
    texts: dict[str, str] = {}
    if not wanted:
        return texts
    proc = subprocess.run(
        ["git", "-C", str(clone), "cat-file", "--batch"],
        input="".join(f"{sha}\n" for sha, _ in wanted).encode(),
        capture_output=True,
        check=True,
    ).stdout
    at = 0
    for _, path in wanted:
        end = proc.index(b"\n", at)
        size = int(proc[at:end].split()[2])
        blob = proc[end + 1 : end + 1 + size]
        at = end + 1 + size + 1
        if b"\0" not in blob[:8000]:
            texts[path] = blob.decode("utf-8", "replace")
    return texts


def rank_files(clone: Path, commit: str, paths: set[str], query: str, limit: int) -> list[str]:
    key = (str(clone), commit)
    if key not in _CACHE:
        _CACHE.clear()  # one checkout at a time
        _CACHE[key] = Index(read_files(clone, commit, paths))
    return _CACHE[key].rank(query, limit)
