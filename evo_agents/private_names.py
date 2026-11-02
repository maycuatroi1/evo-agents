"""The private names a hub keeps out of public repos, and the matcher every guard of them shares (core plugin guard,
``docs/guard.md``).

A project lists names that must never reach a public repo (its own name, hosts, domains, groups, repos) and the repos
it publishes; the hub hands every worker and member the union, as a policy, and their guards refuse a commit, a push
or the text of a pull request, issue or release bound for a guarded repo that holds one of the names.

The policy carries no name in clear: each term is the hash of its words, so the file a worker keeps, a log line or an
agent that reads the file never spells one. A text is split into words (runs of letters and digits, lowercased after
NFKC), and a term of N words matches N words in a row whose hash is the term's, each two apart by at most three
spaces, tabs or ``-_./:@`` (``GAP``), never across a line. A term of one word of at least
``PART_MIN`` characters also matches inside a longer word (``acmeharness``, ``AcmeCLI``), so a name joined to another
is still found; its line in the policy carries the word's length, so a guard hashes only the parts of that length.

Standard library only, and outside ``evo_agents.hub``: the worker's git hooks and ``gh`` shim, the evo-hub plugin's hook
and the hub all import it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field

VERSION = 1  # the shape of a policy (``Policy.to_json``)
PART_MIN = 5  # a one-word term this long or longer also matches inside a longer word
MAX_TERM_WORDS = 8
MAX_TERM_CHARS = 200
SALT = b"evo-guard/1\n"  # in front of every hashed term, so a hash here is no plain SHA of a word
WORD = re.compile(r"[^\W_]+")  # letters and digits; "_", "-", "." and spaces split words
SNIPPET = 40  # characters of context on each side of a hit, for the message that refuses
GAP = re.compile(r"[ \t\-_./:@]{1,3}")  # what may stand between the words of a term in a text: never a line break


def normalized(text: str) -> str:
    """The NFKC form of ``text``, which the positions of words and hits count in."""
    return unicodedata.normalize("NFKC", text)


def words(text: str) -> list[tuple[str, int, int]]:
    """Each word of ``text``, lowercased, with its start and end in the NFKC form of the text."""
    norm = normalized(text)
    return [(m.group().lower(), m.start(), m.end()) for m in WORD.finditer(norm)]


def term_words(term: str) -> tuple[str, ...]:
    """The words of a term, which its hash is made of; ValueError for a term without one, or too long."""
    if len(term) > MAX_TERM_CHARS:
        raise ValueError(f"a term has at most {MAX_TERM_CHARS} characters")
    found = tuple(w for w, _, _ in words(term))
    if not found:
        raise ValueError("a term needs at least one letter or digit")
    if len(found) > MAX_TERM_WORDS:
        raise ValueError(f"a term has at most {MAX_TERM_WORDS} words")
    return found


def term_hash(parts: tuple[str, ...] | list[str]) -> str:
    return hashlib.blake2b(SALT + " ".join(parts).encode(), digest_size=16).hexdigest()


def entries(term: str) -> list[dict]:
    """The policy's lines for a term: its hash, how many words it has, and for a term matched inside words too, the
    length of its one word (``part``, 0 for none), so a guard hashes only parts of that length. A term of several
    words gets a second line for its words written together (``acme-ops`` also as ``acmeops``)."""
    parts = term_words(term)
    if len(parts) == 1:
        size = len(parts[0])
        return [{"h": term_hash(parts), "n": 1, "part": size if size >= PART_MIN else 0}]
    out = [{"h": term_hash(parts), "n": len(parts), "part": 0}]
    joined = "".join(parts)
    if len(joined) >= PART_MIN:
        out.append({"h": term_hash([joined]), "n": 1, "part": len(joined)})
    return out


def repo_key(url: str) -> str:
    """``host/owner/repo`` of a git remote or a GitHub slug, lowercased: ``git@github.com:o/r.git``,
    ``https://user:token@github.com/o/r``, ``ssh://git@host:2222/g/sub/r`` and ``o/r`` (GitHub) all read."""
    u = url.strip()
    if m := re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+)$", u):
        host, path = m.group(1), m.group(2)
    elif m := re.match(r"^(?:[^@/]+@)?([^/:]+):(?!\d+/)(.+)$", u):  # scp-like, git@host:owner/repo
        host, path = m.group(1), m.group(2)
    elif re.match(r"^[^/\s]+/[^/\s]+$", u):
        host, path = "github.com", u
    else:
        return u.lower()
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return f"{host}/{path}".lower()


@dataclass(frozen=True)
class Hit:
    """A private name found in a text: where, and the text around it, with the name itself shown as it was written."""

    start: int
    end: int
    found: str
    context: str


@dataclass
class Policy:
    """The terms (hashed) and the guarded repos (``host/owner/repo`` patterns, ``*`` and ``?`` as in fnmatch)."""

    terms: list[dict] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> Policy:
        if data.get("version") != VERSION:
            raise ValueError(f"policy version {data.get('version')!r}, this guard reads {VERSION}")
        terms = [
            {"h": str(t["h"]), "n": int(t["n"]), "part": int(t.get("part") or 0)}
            for t in data.get("terms") or []
            if 1 <= int(t["n"]) <= MAX_TERM_WORDS
        ]
        return cls(terms=terms, targets=[str(p).lower() for p in data.get("targets") or []])

    def to_json(self) -> dict:
        terms = sorted(self.terms, key=lambda t: t["h"])
        return {"version": VERSION, "terms": terms, "targets": sorted(set(self.targets)), "digest": self.digest()}

    def digest(self) -> str:
        body = json.dumps(
            [sorted((t["h"], t["n"], t["part"]) for t in self.terms), sorted(set(self.targets))], separators=(",", ":")
        )
        return hashlib.sha256(body.encode()).hexdigest()

    def guards(self, url: str) -> bool:
        """Whether the repo of ``url`` is one the policy guards."""
        key = repo_key(url)
        return any(fnmatch.fnmatchcase(key, pattern) for pattern in self.targets)

    def matcher(self) -> Matcher:
        return Matcher(self.terms)


class Matcher:
    """Finds the policy's terms in a text by hashing its runs of words, and the parts of its long words."""

    def __init__(self, terms: list[dict]) -> None:
        self.whole: dict[int, set[str]] = {}  # by the number of words
        self.part: dict[int, set[str]] = {}  # by the length of the one word
        for t in terms:
            self.whole.setdefault(t["n"], set()).add(t["h"])
            if t["part"]:
                self.part.setdefault(t["part"], set()).add(t["h"])

    def __bool__(self) -> bool:
        return bool(self.whole)

    def find(self, text: str, *, limit: int = 20) -> list[Hit]:
        norm = normalized(text)
        found = words(text)
        hits: list[Hit] = []
        seen: set[tuple[int, int]] = set()

        def hit(start: int, end: int) -> None:
            if (start, end) in seen or len(hits) >= limit:
                return
            seen.add((start, end))
            context = norm[max(0, start - SNIPPET) : end + SNIPPET].replace("\n", " ")
            hits.append(Hit(start, end, norm[start:end], context))

        for n, hashes in self.whole.items():
            for i in range(len(found) - n + 1):
                run = found[i : i + n]
                if term_hash([w for w, _, _ in run]) not in hashes:
                    continue
                if all(GAP.fullmatch(norm[a[2] : b[1]]) for a, b in zip(run, run[1:], strict=False)):
                    hit(run[0][1], run[-1][2])
        for size, hashes in self.part.items():
            for w, start, _ in found:
                for i in range(len(w) - size + 1 if len(w) > size else 0):
                    if term_hash([w[i : i + size]]) in hashes:
                        hit(start + i, start + i + size)
        hits.sort(key=lambda h: h.start)
        return hits


def spans(hits: list[Hit]) -> list[tuple[int, int]]:
    """The places of ``hits`` in the NFKC text, those that overlap joined, in order."""
    out: list[tuple[int, int]] = []
    for h in sorted(hits, key=lambda h: (h.start, h.end)):
        if out and h.start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], h.end))
        else:
            out.append((h.start, h.end))
    return out


def refusal(hits: list[Hit], where: str) -> str:
    """The words that refuse a text: which part held a private name, and the text around each."""
    lines = [
        f"evo guard: {where} holds {len(hits)} private name(s) of a hub project, and goes to a public repo.",
        "Write a fictional name instead (northwind, lumen-*, lumenops.example); the hub's guard plugin lists the names.",
    ]
    lines += [f"  - {h.found!r} in: ...{h.context}..." for h in hits[:10]]
    return "\n".join(lines)
