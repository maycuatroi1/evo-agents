"""An in-RAM inverted index that scores as SQLite's FTS5 ``bm25`` does: what ``evo_agents.kg.snapshot_text`` reads
the full-text rows of a snapshot with, in place of ``node_fts``, ``node_fts_aud``, ``node_tri`` and ``code_fts``.

- Tokens are cut as FTS5's tokenizers cut them: ``unicode61 remove_diacritics 2`` keeps runs of letters and
  digits, lower-cased, accents removed (``words``); with ``tokenchars '_-$'`` (``code_fts``) those three join a word
  too (``code_words``); the trigram tokenizer (``node_tri``) reads every three characters of a text.
- A query is the subset of FTS5's syntax ``evo_agents.kg.search`` writes: quoted phrases, a phrase's last word as a
  prefix (``"word"*``), AND, OR and parentheses, AND binding tighter than OR (``parse``).
- A row's score is FTS5's: over the phrases of the query, IDF times f * (k1 + 1) / (f + k1 * (1 - b + b * D /
  avgdl)), k1 1.2 and b 0.75, f the phrase's hits in each column times the column's weight, D the row's tokens, IDF
  ln((N - n + 0.5) / (n + 0.5)) over the N rows of the table, n of which hold the phrase, or 1e-6 when that is not
  above 0. The score is negated, lower is better, as ``bm25()`` returns it."""

from __future__ import annotations

import math
import re
import unicodedata
from array import array
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from evo_agents.kg.pacing import paced, paced_sorted

K1 = 1.2
B = 0.75
_WORD = re.compile(r"[^\W_]+")
_CODE_WORD = re.compile(r"[\w$-]+")


@lru_cache(maxsize=1 << 16)
def _bare(token: str) -> str:
    """``token`` lower-cased, accents removed; the words of a language repeat, so each is worked out once."""
    decomposed = unicodedata.normalize("NFD", token.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    """The tokens ``unicode61 remove_diacritics 2`` cuts ``text`` into. An ASCII text is lower-cased whole, which
    moves no boundary between tokens, and cut in one call: most of a graph's text is."""
    if text.isascii():
        return _WORD.findall(text.lower())
    return [t for t in (_bare(w) if not w.isascii() else w.lower() for w in _WORD.findall(text)) if t]


def code_words(text: str) -> list[str]:
    """The tokens of ``code_fts``'s tokenizer: ``_``, ``-`` and ``$`` inside a word."""
    if text.isascii():
        return _CODE_WORD.findall(text.lower())
    return [t for t in (_bare(w) if not w.isascii() else w.lower() for w in _CODE_WORD.findall(text)) if t]


def trigrams(text: str) -> list[str]:
    """The tokens of the trigram tokenizer: every three characters of ``text``, case folded."""
    low = text.lower()
    return [low[i : i + 3] for i in range(len(low) - 2)]


# -- a query ---------------------------------------------------------------------------------------------------------

_TOKEN = re.compile(r'\s*(?:(?P<phrase>"(?:[^"]|"")*")(?P<star>\*)?|(?P<op>AND|OR)\b|(?P<paren>[()]))')


@dataclass(frozen=True)
class Phrase:
    text: str  # as quoted, without the quotes
    prefix: bool


class QueryError(ValueError):
    """A query this index cannot read: FTS5 would fail on it too, and a search of it gives nothing."""


def parse(query: str) -> tuple[list[Phrase], tuple]:
    """The phrases of an FTS5 query in order, each as often as written, and its tree: ("p", phrase number), ("and",
    children) or ("or", children)."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(query):
        m = _TOKEN.match(query, pos)
        if m is None or m.end() == pos:
            if query[pos:].strip():
                raise QueryError(query)
            break
        pos = m.end()
        if m.group("phrase"):
            tokens.append(("p", m.group("phrase")[1:-1].replace('""', '"') + ("*" if m.group("star") else "")))
        elif m.group("op"):
            tokens.append(("op", m.group("op")))
        else:
            tokens.append(("paren", m.group("paren")))
    phrases: list[Phrase] = []
    tree, end = _or(tokens, 0, phrases)
    if end != len(tokens):
        raise QueryError(query)
    return phrases, tree


def _or(tokens, i: int, phrases: list[Phrase]) -> tuple[tuple, int]:
    parts = []
    node, i = _and(tokens, i, phrases)
    parts.append(node)
    while i < len(tokens) and tokens[i] == ("op", "OR"):
        node, i = _and(tokens, i + 1, phrases)
        parts.append(node)
    return (parts[0] if len(parts) == 1 else ("or", tuple(parts))), i


def _and(tokens, i: int, phrases: list[Phrase]) -> tuple[tuple, int]:
    parts = []
    node, i = _atom(tokens, i, phrases)
    parts.append(node)
    while i < len(tokens) and (tokens[i] == ("op", "AND") or tokens[i][0] == "p" or tokens[i] == ("paren", "(")):
        if tokens[i] == ("op", "AND"):
            i += 1
        node, i = _atom(tokens, i, phrases)
        parts.append(node)
    return (parts[0] if len(parts) == 1 else ("and", tuple(parts))), i


def _atom(tokens, i: int, phrases: list[Phrase]) -> tuple[tuple, int]:
    if i >= len(tokens):
        raise QueryError("a query that ends early")
    kind, value = tokens[i]
    if kind == "p":
        prefix = value.endswith("*")
        phrases.append(Phrase(value[:-1] if prefix else value, prefix))
        return ("p", len(phrases) - 1), i + 1
    if (kind, value) == ("paren", "("):
        node, i = _or(tokens, i + 1, phrases)
        if i >= len(tokens) or tokens[i] != ("paren", ")"):
            raise QueryError("a parenthesis left open")
        return node, i + 1
    raise QueryError(f"unexpected {value!r}")


def matching(tree: tuple, hits: Sequence[dict[int, tuple]]) -> set[int]:
    """The rows ``tree`` matches, given each phrase's hits (row: hits per column)."""
    if tree[0] == "p":
        return set(hits[tree[1]])
    parts = [matching(child, hits) for child in tree[1]]
    if tree[0] == "and":
        return set.intersection(*parts)
    return set.union(*parts)


# -- a table ---------------------------------------------------------------------------------------------------------


class Table:
    """The rows of one FTS5 table: per row the text of each indexed column, per token the rows and how often each
    column holds it, each row's tokens and the table's. ``tokenize`` cuts a text as the table's tokenizer does."""

    def __init__(self, rows: Sequence[Sequence[str]], tokenize: Callable[[str], list[str]]):
        self.rows = tuple(tuple(r) for r in paced(rows))
        self.tokenize = tokenize
        self.columns = columns = len(self.rows[0]) if self.rows else 0
        # per token, one flat array of records: a row holding it, then how often each column does, rows ascending.
        # A graph's tables hold millions of (token, row) pairs: a dict and a tuple per pair made the snapshot millions
        # of objects, minutes of the api's CPU to build on the hub, and gigabytes to keep.
        postings: dict[str, array] = {}
        sizes = array("I")
        for n, row in enumerate(paced(self.rows)):
            size = 0
            held: dict[str, list[int]] = {}
            for c, text in enumerate(row):
                tokens = tokenize(text or "")
                size += len(tokens)
                for t, f in Counter(tokens).items():
                    counts = held.get(t)
                    if counts is None:
                        counts = held[t] = [0] * columns
                    counts[c] = f
            for t, counts in held.items():
                found = postings.get(t)
                if found is None:
                    found = postings[t] = array("I")
                found.append(n)
                found.extend(counts)
            sizes.append(size)
        self.postings = postings
        self.vocabulary = paced_sorted(postings)
        self.sizes = sizes
        self.total = sum(sizes)

    def __len__(self) -> int:
        return len(self.rows)

    def _held(self, token: str) -> dict[int, tuple]:
        """Each row holding ``token``: how often each of its columns holds it."""
        found = self.postings.get(token)
        if found is None:
            return {}
        width = self.columns + 1
        return {found[i]: tuple(found[i + 1 : i + width]) for i in range(0, len(found), width)}

    def _rows_of(self, token: str) -> array:
        """The rows holding ``token``, ascending."""
        found = self.postings.get(token)
        return array("I") if found is None else found[:: self.columns + 1]

    def _tokens(self, prefix: str) -> list[str]:
        start = bisect_left(self.vocabulary, prefix)
        out = []
        for t in self.vocabulary[start:]:
            if not t.startswith(prefix):
                break
            out.append(t)
        return out

    def hits(self, phrase: Phrase) -> dict[int, tuple]:
        """Each row holding ``phrase``: how often each of its columns holds it."""
        tokens = self.tokenize(phrase.text)
        if not tokens:
            return {}
        if len(tokens) == 1:
            found = self._tokens(tokens[0]) if phrase.prefix else [tokens[0]] if tokens[0] in self.postings else []
            out: dict[int, tuple] = {}
            for t in found:
                for n, counts in self._held(t).items():
                    have = out.get(n)
                    out[n] = counts if have is None else tuple(a + b for a, b in zip(have, counts, strict=True))
            return out
        return self._sequence(tokens, phrase.prefix)

    def _sequence(self, tokens: list[str], prefix: bool) -> dict[int, tuple]:
        """The rows holding ``tokens`` one after the other in a column, the last as a prefix when ``prefix``."""
        last = self._tokens(tokens[-1]) if prefix else [tokens[-1]]
        candidates: set[int] | None = None
        for t in tokens[:-1]:
            rows = set(self._rows_of(t))
            candidates = rows if candidates is None else candidates & rows
        assert candidates is not None, "a phrase of several tokens has a first"
        ends = set()
        for t in last:
            ends.update(self._rows_of(t))
        out = {}
        for n in candidates & ends:
            counts = tuple(self._occurrences(self.tokenize(text or ""), tokens, prefix) for text in self.rows[n])
            if any(counts):
                out[n] = counts
        return out

    @staticmethod
    def _occurrences(held: list[str], tokens: list[str], prefix: bool) -> int:
        count, width = 0, len(tokens)
        for i in range(len(held) - width + 1):
            window = held[i : i + width]
            if window[:-1] == tokens[:-1] and (
                window[-1].startswith(tokens[-1]) if prefix else window[-1] == tokens[-1]
            ):
                count += 1
        return count

    def idf(self, held: int) -> float:
        value = math.log((len(self.rows) - held + 0.5) / (held + 0.5))
        return value if value > 0 else 1e-6

    def score(self, n: int, hits: Sequence[dict[int, tuple]], idfs: Sequence[float], weights: Sequence[float]) -> float:
        """FTS5's bm25 of row ``n`` for phrases with ``hits`` and ``idfs``, columns weighed by ``weights``."""
        avgdl = self.total / len(self.rows) if self.rows else 0.0
        norm = K1 * (1 - B + B * self.sizes[n] / avgdl) if avgdl else K1 * (1 - B)
        score = 0.0
        for found, idf in zip(hits, idfs, strict=True):
            counts = found.get(n)
            if counts is None:
                continue
            f = sum(w * c for w, c in zip(weights, counts, strict=False))
            score += idf * (f * (K1 + 1)) / (f + norm)
        return -score

    def search(
        self, query: str, weights: Sequence[float], keep: Callable[[int], bool] | None = None
    ) -> list[tuple[int, float]]:
        """(row, bm25) of the rows matching ``query`` and kept by ``keep``, by row; [] for a query FTS5 cannot read."""
        try:
            phrases, tree = parse(query)
        except QueryError:
            return []
        hits = [self.hits(p) for p in phrases]
        idfs = [self.idf(len(h)) for h in hits]
        return [
            (n, self.score(n, hits, idfs, weights)) for n in sorted(matching(tree, hits)) if keep is None or keep(n)
        ]
