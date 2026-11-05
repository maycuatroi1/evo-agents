"""What the entities step (``evo_agents.kg.entities``) asks its model about the answers of a whole build, as LightRAG
does at merge time (plan kg-entity-relation-view, a2): which entities are one, and one description of what many items
say of an entity or a relation.

- Candidates. Two nodes of the build are a candidate pair when they are near in vectors (the ``synonym_of`` pairs of
  ``entity_links.synonyms``, when the graph has vectors), when they share a name in two kinds, or when the names of two
  nodes of one kind are near: the words of one are all words of the other, or the two read alike (``difflib``, NEAR
  and above). At most MAX_PAIRS, the first two reasons first.
- Merge. The model is asked about the pairs, MERGE_BATCH a call, with the name, kind and description each node has in
  its most public item; it answers per pair whether they are one entity and why. Each answer is memoized by the pair
  (kinds and names) and the model, so a later build asks only about new pairs. A pair the model calls one is folded
  by ``entity_links.group`` (``joins``), and the step states an edge MERGE_REL from the node that stays to the
  document of the most public item of the node folded in, with the model's reason: a writer who deprecates that edge
  (a decision of kind deprecate, ``evo_agents.kg.pipeline.curation``) undoes the merge at the next build, and the edge
  stays, marked deprecated, so the decision keeps applying (``undone``).
- Aggregation. Every item that states a node or a relation is in the group of its label: the items of one label, and
  only those, are read together. A relation stated more than once in a group (after the merge, and in the lightrag
  style in either direction) gets the sum of its strengths as ``weight`` and the union of its keywords; a node or a
  relation whose group holds SUMMARY_FRAGMENTS descriptions or more, or more than SUMMARY_TOKENS tokens of them, gets
  a ``summary`` the model writes of that group's descriptions alone, memoized by the descriptions and the model. A
  summary therefore mixes the descriptions of one label and carries that label, the highest of its sources; a group
  without one keeps its ``descriptions``. Each item's node or edge, and its edge ``mentions`` to a node, carries what
  its own group has. The graph shows on a node or a relation the props of the most public item that states it
  (``evo_agents.kg.assembly``); the ``mentions`` edges, each labelled as its item, keep every group's, so a reader
  finds there the summary of a label they read, else the descriptions they read.

Every call goes through the step's ceiling and sink (``entities._Step.task``): an item's text reaches the model only
when the sink clears its label, a call past the ceiling waits for a later build (``pending``), and a verification
reads the memo only.
"""

from __future__ import annotations

import difflib
from collections import defaultdict
from dataclasses import dataclass

from evo_agents.kg import entity_links as links
from evo_agents.kg.protocol import canonical_json, sha256

MERGE_REL = "merged_from"
MERGE_STAGE = "entity_merge"
MERGE_VERSION = "1"
SUMMARY_STAGE = "entity_summary"
SUMMARY_VERSION = "1"
MERGE_BATCH = 20  # pairs a call
MAX_PAIRS = 2000  # candidate pairs a build asks about at most
NEAR = 0.85  # how alike two names of one kind read (difflib) to be a candidate
MAX_BUCKET = 50  # a word more nodes of one kind share than this pairs none of them
SUMMARY_FRAGMENTS = 8  # LightRAG's force_llm_summary_on_merge
SUMMARY_TOKENS = 1200  # LightRAG's summary_max_tokens; 4 characters a token
MAX_SUMMARY = 1200  # characters of a summary kept
MAX_REASON = 300
MAX_DESCRIPTIONS = 20  # descriptions a group without a summary keeps
MAX_KEYWORDS = 12
WHY = ("vectors", "same name", "near names")
TASKS = ("merge", "summary")  # what ``entities.call_task`` asks a command, as ``task``
ANSWER_KEYS = ("decisions", "summary")  # what an answer to a task holds besides its usage
SYSTEM = (
    "You decide whether names are one entity and merge descriptions, and answer with one JSON object, nothing else."
)


@dataclass(frozen=True)
class Seen:
    """A node as its most public item states it: what the model is shown of it, and that item."""

    kind: str
    name: str
    description: str
    item: str
    label: object


def merge_on(config: dict) -> bool:
    """Whether the step asks the model to merge and summarize: the mapping's ``merge``, by default on in the lightrag
    style only, so a closed mapping builds as it did."""
    found = config.get("merge")
    return found if isinstance(found, bool) else (config.get("style") or "closed") == "lightrag"


# -- candidates ---------------------------------------------------------------------------------------------------


def candidates(groups: links.Groups, synonym_pairs, limit: int = MAX_PAIRS) -> list[tuple[str, str, str]]:
    """(smaller id, larger id, why) of the pairs of nodes the model is asked about, in a stable order."""
    found: dict[tuple[str, str], tuple[int, float, str]] = {}

    def add(a: str, b: str, why: int, score: float) -> None:
        if a != b:
            pair = (min(a, b), max(a, b))
            if pair not in found or (why, -score) < found[pair][:2]:
                found[pair] = (why, -score, WHY[why])

    for a, b, cos in synonym_pairs:
        if a in groups.name and b in groups.name:
            add(a, b, 0, float(cos))
    by_key: dict[str, set[str]] = defaultdict(set)
    for (_kind, k), nid in groups.of.items():
        by_key[k].add(nid)
    for nids in by_key.values():
        ordered = sorted(nids)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                if groups.kind[a] != groups.kind[b]:
                    add(a, b, 1, 1.0)
    for a, b, score in _near(groups):
        add(a, b, 2, score)
    ranked = sorted(found.items(), key=lambda x: (x[1][0], x[1][1], x[0]))[:limit]
    return [(a, b, why) for (a, b), (_, _, why) in ranked]


def _near(groups: links.Groups):
    """(a, b, how alike) of the nodes of one kind whose names are near: the words of one all in the other, or alike.
    Only names that share a word or their first four letters are compared."""
    words: dict[str, list[str]] = {}
    bucket: dict[tuple[str, str], list[str]] = defaultdict(list)
    for nid in sorted(groups.name):
        k = links.key(groups.name[nid])
        words[nid] = k.split("-") if k else []
        for w in set(words[nid]):
            if len(w) >= 3:
                bucket[(groups.kind[nid], w)].append(nid)
        if len(k) >= 4:  # two spellings of one word share no word, but their start
            bucket[(groups.kind[nid], "^" + k[:4])].append(nid)
    seen: set[tuple[str, str]] = set()
    for nids in bucket.values():
        if len(nids) > MAX_BUCKET:
            continue
        for i, a in enumerate(nids):
            for b in nids[i + 1 :]:
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                wa, wb = set(words[a]), set(words[b])
                alike = difflib.SequenceMatcher(None, "-".join(words[a]), "-".join(words[b])).ratio()
                if wa < wb or wb < wa or alike >= NEAR:
                    yield a, b, round(alike, 4)


# -- the model's answers ------------------------------------------------------------------------------------------


def pair_inputs(a: Seen, b: Seen, model: str) -> dict:
    """What a pair's answer is memoized by: the two (kind, name), in order, and the model."""
    one, two = sorted([f"{a.kind}:{a.name}", f"{b.kind}:{b.name}"])
    return {"a": one, "b": two, "model": model}


def merge_prompt(pairs: list[tuple[Seen, Seen]]) -> str:
    lines = []
    for i, (a, b) in enumerate(pairs, 1):
        lines.append(f"{i}. A: {_shown(a)}\n   B: {_shown(b)}")
    return (
        "For each numbered pair, decide whether A and B name the same real-world entity (one person, place, thing or "
        "idea), not merely related ones. Answer with one JSON object, nothing else: "
        '{"decisions": [{"pair": 1, "same": true, "reason": "one short sentence"}]}, one decision per pair.\n\n'
        "PAIRS:\n" + "\n".join(lines)
    )


def _shown(s: Seen) -> str:
    return f"{s.name} ({s.kind})" + (f": {s.description}" if s.description else "")


def read_decisions(answer: dict, count: int) -> dict[int, dict]:
    """The pair number (from 1) -> {"same", "reason"} of an answer, those that read."""
    out = {}
    for d in answer.get("decisions") or []:
        if not isinstance(d, dict) or isinstance(d.get("pair"), bool) or not isinstance(d.get("pair"), int):
            continue
        if 1 <= d["pair"] <= count and isinstance(d.get("same"), bool):
            out[d["pair"]] = {"same": d["same"], "reason": str(d.get("reason") or "")[:MAX_REASON]}
    return out


def summary_inputs(subject: str, fragments: list[str], model: str) -> dict:
    return {"subject": subject, "fragments": sha256(canonical_json(sorted(fragments))), "model": model}


def summary_prompt(subject: str, fragments: list[str]) -> str:
    return (
        f"These are descriptions of {subject}, each taken from a different text. Write one description of it in at "
        "most four sentences that keeps what they say and adds nothing they do not; where they disagree, say so. "
        'Answer with one JSON object, nothing else: {"summary": "..."}.\n\nDESCRIPTIONS:\n'
        + "\n".join(f"- {f}" for f in fragments)
    )


def read_summary(answer: dict) -> str | None:
    found = answer.get("summary")
    return found.strip()[:MAX_SUMMARY] if isinstance(found, str) and found.strip() else None


def needs_summary(fragments: list[str]) -> bool:
    return len(fragments) >= SUMMARY_FRAGMENTS or sum(len(f) for f in fragments) / 4 > SUMMARY_TOKENS


def merged_keywords(said: list[str]) -> str | None:
    """The keywords of the statements of one relation, each once (folded), in the order first stated."""
    out: dict[str, str] = {}
    for text in said:
        for word in text.split(","):
            w = word.strip()
            if links.key(w):
                out.setdefault(links.key(w), w)
    return ", ".join(list(out.values())[:MAX_KEYWORDS]) or None


def undone(records, body) -> set[tuple[str, str]]:
    """The (from, to) of the MERGE_REL edges the active decisions of the build deprecate: merges a writer undid."""
    import yaml

    from evo_agents.kg.pipeline.curation import is_curation

    out = set()
    for rec in records:
        if rec.record.get("kind") != "binding" or not is_curation(rec.record):
            continue
        try:
            data = yaml.safe_load(body(rec) or "") or {}
        except yaml.YAMLError:
            continue
        for entry in data.get("decisions") or [] if isinstance(data, dict) else []:
            if isinstance(entry, dict) and entry.get("kind") == "deprecate" and entry.get("rel") == MERGE_REL:
                out.add((str(entry.get("from") or ""), str(entry.get("to") or "")))
    return out


__all__ = [
    "MERGE_REL",
    "Seen",
    "candidates",
    "merge_on",
    "merge_prompt",
    "merged_keywords",
    "needs_summary",
    "read_decisions",
    "read_summary",
    "summary_prompt",
    "undone",
]
