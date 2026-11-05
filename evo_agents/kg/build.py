"""``evo-agents kg build``: the corpus state in, a graph snapshot out.

Stages run as memoized tasks: map and structure per item, then one identifier index, then link and
binding per item. Assembly merges facts, applies entity resolution (only declared ``same_as``),
evaluates the provenance circuit, computes labels, checks node keys, and writes the store as one build.
Its coverage names, per source, what the build holds; for a git source that includes the commit read
(``evo_agents.kg.freshness``).
A store written by an older schema is rebuilt from the corpus log into a new file that replaces it.

A build does only what changed. A link task is done again only for an item whose own text or structure changed or
that read an entry of the identifier index that now holds something else (``Memo.run_traced``), and the store
rewrites only the rows of the full-text index that changed. A build whose corpus state and inputs (the code, the
config, the cursors: ``build_inputs``) are those of the store's latest build is not done at all (``unchanged``).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents import __version__
from evo_agents.kg import audit, entities, entity_map, strategy
from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg import review as kg_review
from evo_agents.kg import search as text_search
from evo_agents.kg import store as kg_store
from evo_agents.kg.assembly import Facts, assemble
from evo_agents.kg.corpus import Corpus
from evo_agents.kg.freshness import add_reads
from evo_agents.kg.ids import utc_now
from evo_agents.kg.memo import Memo
from evo_agents.kg.pipeline import curation
from evo_agents.kg.pipeline.binding import BINDING_VERSION
from evo_agents.kg.pipeline.link import LINK_VERSION, link_traced
from evo_agents.kg.pipeline.link_index import build_index, index_digest
from evo_agents.kg.pipeline.link_reads import Held
from evo_agents.kg.pipeline.stages import (
    DIRECTORY_CONNECTORS,
    MAP_VERSION,
    STRUCTURE_VERSION,
    code_coverage,
    code_extractor,
    declares,
    frag_unit,
    item_unit,
    map_item,
    missing_graphify,
    structure_item,
)
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.profile import PROFILE_VERSION, build_profile, item_stats
from evo_agents.kg.project import Project, ProjectError
from evo_agents.kg.protocol import canonical_json, sha256
from evo_agents.kg.schema import known_kinds, known_relations
from evo_agents.kg.settings import DEFAULT as DEFAULT_TUNING
from evo_agents.kg.settings import SettingsError, Tuning
from evo_agents.kg.settings import read as read_tuning
from evo_agents.kg.store import Graph, StaleSchema, Store
from evo_agents.kg.store_clusters import Clusters
from evo_agents.kg.store_filters import label_columns
from evo_agents.kg.text import split_passages

# config: the aliases of tsconfig.json; commit and pr: the files a commit changed, the commit a pull request merged as
STRUCTURED_KINDS = {"manifest", "contracts", "plan", "code", "markdown", "config", "commit", "pr"}
DEFINITION_SAMPLES = 15
# Their text reaches node_fts through symbols, docstrings, plans and steps; code and markup reach code_fts whole.
UNSEARCHED_ITEMS = ("code", "plan", "markup")
CODE_TEXT_ITEMS = ("code", "markup")  # the items whose text kg_search looks up words in (``kg.search``, code_fts)


@dataclass
class BuildReport:
    project: str
    ok: bool
    build_id: int | None = None
    content_hash: str | None = None
    nodes: int = 0
    edges: int = 0
    units: int = 0
    dangling_edges: int = 0
    errors: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # coverage gaps a build survives, e.g. a missing extra
    coverage: dict = field(default_factory=dict)
    memo: dict = field(default_factory=dict)
    seconds: float = 0.0
    verify: dict | None = None
    rebuilt_from: str | None = None  # the older store schema this build replaced, if any
    skipped: bool = False  # nothing changed since build ``build_id``, which stays the latest (``unchanged``)

    def summary_line(self) -> str:
        if not self.ok:
            return f"build FAILED for {self.project}: " + "; ".join(self.errors[:5])
        if self.skipped:
            return (
                f"build {self.build_id} for {self.project} is current: {self.nodes} nodes, {self.edges} edges;"
                " the corpus and the config have not changed since, so nothing was built"
            )
        rebuilt = (
            f", store rebuilt from the log (schema {self.rebuilt_from} -> {kg_store.SCHEMA_VERSION})"
            if self.rebuilt_from
            else ""
        )
        return (
            f"build {self.build_id} for {self.project}: {self.nodes} nodes, {self.edges} edges, {self.units} units"
            f" in {self.seconds}s (memo {sum(s['hits'] for s in self.memo.values())} hits,"
            f" {sum(s['misses'] for s in self.memo.values())} misses){rebuilt}"
        )

    def to_json(self) -> dict:
        return self.__dict__


def run_pipeline(
    project: Project,
    corpus: Corpus,
    memo: Memo,
    *,
    entity_memo: Memo | None = None,
    offline: bool = False,
    entity_outbox: list | None = None,
    entity_inbox: dict | None = None,
    entity_keys: dict | None = None,
) -> tuple[Graph, dict, list[str], list[str]]:
    """The facts of the corpus, the graph made of them and its coverage. ``entity_memo`` is where the entities step
    (``kg.entities``) keeps what its model said (the build's memo by default); ``offline`` makes that step read it
    without calling the model: how a build is verified. ``entity_outbox`` and ``entity_inbox`` are the step's
    ``outbox`` and ``inbox``: the requests it defers on a machine without ``claude``, the answers given elsewhere;
    ``entity_keys`` its ``keys``, the key of a url model held in memory."""
    try:
        ontology = project.ontology
    except ProjectError as exc:
        return Graph(), {}, [str(exc)], []
    # Without an ontology key every kind passes, as before ontologies were checked.
    kinds = known_kinds(ontology) if ontology is not None else None
    sources = {s["id"]: s for s in project.sources()}
    backends = {sid: (s.get("code") or {}).get("backend", "auto") for sid, s in sources.items()}
    missing = _missing_backends(backends)
    if missing:
        return Graph(), {}, missing, []
    identifiers = identifiers_of(project)
    records = [r for r in corpus.read_state() if r.source in sources]
    graph = Graph()
    unit_labels = add_units(graph, records, project.policy)
    texts_cache: dict[str, str | None] = {}

    def body(rec) -> str | None:
        if rec.item_id not in texts_cache:
            texts_cache[rec.item_id] = corpus.body_text(rec.record)
        return texts_cache[rec.item_id]

    first, structure_out, code_items = map_items(records, sources, backends, identifiers, memo, body)
    index = build_index(first, identifiers)
    second = link_items(records, structure_out, index, memo, corpus, body)
    tuning = tuning_of(project)
    extracted: dict[str, list[dict]] = {}
    entities_coverage = None
    if tuning.entities:
        config = entities.enabled(tuning, getattr(getattr(project, "harness", None), "root", None))
        if config is None:
            entities_coverage = entities.not_enabled(tuning)
        else:
            extracted, entities_coverage = entities.run(
                config,
                project,
                records,
                body,
                entity_memo or memo,
                unit_labels,
                first,
                kinds,
                known_relations(ontology) if ontology is not None else None,
                offline=offline,
                semantic_settings=_semantic_of(tuning),
                outbox=entity_outbox,
                inbox=entity_inbox,
                keys=entity_keys,
            )
    mapped: dict[str, list[dict]] = {}
    map_coverage = None
    harness_root = getattr(getattr(project, "harness", None), "root", None)
    if "entity_map" in strategy.steps_of(tuning.strategy, harness_root):
        maps, map_errors = entity_map.read_maps(project.root)
        node_kinds = _map_kinds(tuning.strategy, harness_root, kinds)
        mapped, map_coverage = entity_map.run(maps, records, unit_labels, _nodes_of(first, second), node_kinds)
        map_coverage["errors"] = map_errors

    facts = Facts(kinds)
    for rec in records:
        facts.add_item(
            rec,
            first[rec.item_id] + second[rec.item_id] + extracted.get(rec.item_id, []) + mapped.get(rec.item_id, []),
        )
    dangling = assemble(graph, facts, unit_labels)
    cut = search_texts(graph, records, corpus, body, facts.notes, tuning_of(project))
    per_source = [_source_coverage(sid, facts, backends[sid], code_items[sid]) for sid in sorted(sources)]
    coverage = {
        "sources": per_source,
        "dangling_edges": dangling,
        "issues": facts.issues[:50],
        "errors": facts.errors[:50],
        "warnings": [
            f"source {e['id']}: {e['code']['warning']}" for e in per_source if e.get("code", {}).get("warning")
        ],
        "multiple_definitions": multiple_definitions(graph, {i["kind"] for i in identifiers}),
        "search": {"cut": len(cut), "max_chars": text_search.MAX_TEXT_CHARS, "samples": cut[:DEFINITION_SAMPLES]},
    }
    if entities_coverage is not None:
        coverage["entities"] = entities_coverage
    if map_coverage is not None:
        coverage["entity_map"] = map_coverage
        coverage.setdefault("entities", {k: map_coverage[k] for k in ("enabled", "style", "model", "nodes", "edges")})
    coverage["profile"] = build_profile(graph, item_rows(records, memo, body), coverage, project.policy)
    return graph, coverage, facts.errors, facts.issues


def _map_kinds(name: str | None, harness_root, kinds) -> list[str]:
    """The kinds an entity of a map may be: the strategy's that the ontology holds, OTHER always."""
    try:
        said = list(strategy.available(harness_root)[name].ontology.get("node_kinds") or [])
    except (KeyError, strategy.StrategyError):
        said = []
    found = [k for k in said if kinds is None or k in kinds]
    return found if entity_map.OTHER in found else [*found, entity_map.OTHER]


def _nodes_of(first: dict[str, list[dict]], second: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """The node facts of each item, of the map stage and the link stage: what a map may cite."""
    return {
        item: [f for f in first.get(item, []) + second.get(item, []) if f.get("t") == "node"]
        for item in sorted({*first, *second})
    }


def _missing_backends(backends: dict[str, str]) -> list[str]:
    """The errors of the sources that ask for graphify's backend when graphify is not installed."""
    explicit = [sid for sid, backend in sorted(backends.items()) if backend == "graphify-ast"]
    if explicit:
        from evo_agents.kg.extract import graphify as gfy

        if not gfy.available():
            return [missing_graphify(sid) for sid in explicit]
    return []


def identifiers_of(project: Project) -> list[dict]:
    """The identifier patterns of the project's knowledge.yaml, as the structure and link stages read them."""
    return [
        {"kind": i["kind"], "pattern": i["pattern"], **({"aliases": i["aliases"]} if i.get("aliases") else {})}
        for i in project.knowledge.get("identifiers") or []
    ]


def add_units(graph: Graph, records, policy: Policy) -> dict[str, Label]:
    """Put a unit for each item and each of its fragments in ``graph``; the label of each unit."""
    unit_labels: dict[str, Label] = {}
    for rec in records:
        r = rec.record
        label = policy.source_label(rec.source, r.get("label"))
        base = {"item_id": rec.item_id, "rev": r.get("rev"), "source": rec.source, **label_columns(label)}
        graph.units[item_unit(rec.item_id)] = {**base, "anchor": "", "hash": r.get("hash"), "span": None}
        unit_labels[item_unit(rec.item_id)] = label
        for f in r.get("fragments", []):
            uid = frag_unit(rec.item_id, f["anchor"])
            graph.units[uid] = {**base, "anchor": f["anchor"], "hash": f.get("hash"), "span": f.get("span")}
            unit_labels[uid] = label
    return unit_labels


def map_items(
    records, sources: dict[str, dict], backends: dict[str, str], identifiers: list[dict], memo: Memo, body
) -> tuple[dict[str, list[dict]], dict[str, tuple[list[dict], str]], dict[str, list[tuple[dict, str]]]]:
    """Map and structure, per item: the facts of both by item, the structure facts and their hash by item, and the
    code items of each source with the extractor that read them."""
    identifiers_digest = sha256(canonical_json(identifiers))
    first: dict[str, list[dict]] = {}
    structure_out: dict[str, tuple[list[dict], str]] = {}
    code_items: dict[str, list[tuple[dict, str]]] = defaultdict(list)
    for rec in records:
        r = rec.record
        backend = backends[rec.source]
        dirs = sources[rec.source].get("connector") in DIRECTORY_CONNECTORS
        mapped, _ = memo.run(
            "map",
            MAP_VERSION,
            {"item": rec.item_id, "hash": r["hash"], "rev": r.get("rev", ""), "source": rec.source, "dirs": dirs},
            lambda r=r, s=rec.source, d=dirs: map_item(r, s, d),
        )
        if is_structured(r):
            extractor = code_extractor(r, backend)
            if r["kind"] == "code":
                code_items[rec.source].append((r, extractor))
            inputs = {"item": rec.item_id, "hash": r["hash"], "extractor": extractor}
            if r["kind"] in ("markdown", "code"):  # a frontmatter id, a comment, is a code only if a pattern says so
                inputs["identifiers"] = identifiers_digest
            structured, shash = memo.run(
                "structure",
                STRUCTURE_VERSION,
                inputs,
                lambda rec=rec, r=r, b=backend: structure_item(r, rec.source, body(rec), b, identifiers),
            )
        else:
            structured, shash = [], "none"
        structure_out[rec.item_id] = (structured, shash)
        first[rec.item_id] = mapped + structured
    return first, structure_out, code_items


def _source_coverage(sid: str, facts: Facts, backend: str, code_items: list[tuple[dict, str]]) -> dict:
    """The coverage of one source: its counts, samples and resolved ratio, and how its code was read."""
    s = facts.stats[sid]
    entry = {
        "id": sid,
        **s,
        "samples": facts.samples[sid],
        "resolved_ratio": (s["resolved"] / s["mentions"]) if s["mentions"] else 0.0,
    }
    if code_items:
        entry["code"] = code_coverage(backend, code_items)
    return entry


def item_rows(records, memo: Memo, body) -> list[tuple[str, str, dict]]:
    """(source, item id, figures) of each item for the data profile (``evo_agents.kg.profile``); an item's figures are
    memoized by its hash, so a build that changes little reads little."""
    rows = []
    for rec in records:
        r = rec.record
        inputs = {"item": rec.item_id, "hash": r["hash"], "kind": r["kind"]}
        stats, _ = memo.run(
            "profile",
            PROFILE_VERSION,
            inputs,
            lambda rec=rec, r=r: item_stats(r, body(rec), declares(r)),
        )
        rows.append((rec.source, rec.item_id, stats))
    return rows


def link_items(records, structure_out: dict, index: dict, memo: Memo, corpus: Corpus, body) -> dict[str, list[dict]]:
    """Link and binding, per item. A link task is traced (``Memo.run_traced``): it runs again when its item or its
    structure changed, or when an entry of the index it read holds something else. A binding task is keyed by the
    whole index, whose digest is taken only when the project has bindings."""
    held = Held(index)
    idx = None
    second: dict[str, list[dict]] = {}
    for rec in records:
        r = rec.record
        structured, shash = structure_out[rec.item_id]
        if r["kind"] == "binding":
            idx = idx or index_digest(index)
            out, _ = memo.run(
                "binding",
                f"{BINDING_VERSION}.{curation.CURATION_VERSION}",
                {"item": rec.item_id, "hash": r["hash"], "index": idx},
                lambda rec=rec, r=r: curation.bind(r, body(rec), index),
            )
        else:
            out, _ = memo.run_traced(
                "link",
                LINK_VERSION,
                {"item": rec.item_id, "hash": r["hash"], "structure": shash},
                lambda rec=rec, r=r, st=structured: link_traced(r, corpus.fragments(r), body(rec), st, index),
                held.digest,
            )
        second[rec.item_id] = out
    return second


def tuning_of(project: Project) -> Tuning:
    """What the project's settings.json tunes the build by; the defaults when it has none."""
    try:
        return read_tuning(project.root)
    except SettingsError as exc:
        raise ProjectError(str(exc)) from None


def search_texts(
    graph: Graph, records, corpus: Corpus, body, notes: dict[str, list[str]], tuning: Tuning = DEFAULT_TUNING
) -> list[str]:
    """Fill ``graph.texts`` and ``graph.paths``, what the full-text index holds besides names (``kg.search``): the
    sections of a document, the text of an item that is neither code, markup nor a plan (a plan's text is on its plan
    and step nodes), the descriptions and docstrings in ``notes``, and a requirement's code; ``graph.code``, the text
    of a code or markup file without an audience, for ``code_fts``; the path of a node that has
    one, a section's being its document's; and ``graph.passages``, a section or a file read whole longer than
    the project's ``passage_from`` (``kg.settings``) cut into passages with their lines, each after the document's name and the headings
    above it. The ids of the nodes whose body was cut (``kg.search.MAX_TEXT_CHARS``)."""
    parts: dict[str, list[str]] = defaultdict(list)
    long: list[tuple[str, int, str, list[str]]] = []  # (node id, first line, text, context) of a body to cut
    for rec in records:
        r = rec.record
        node = graph.nodes.get(r["id"])
        if node is None:
            continue
        if r.get("fragments") and node["kind"] == "Document":
            _section_texts(graph, r, node, corpus.fragments(r), tuning, parts, long)
        elif r["kind"] not in UNSEARCHED_ITEMS:
            text = body(rec) or ""
            parts[r["id"]].append(text)
            if node["kind"] in ("File", "Document") and len(text) > tuning.passage_from:  # lines of a file
                long.append((r["id"], 1, text, [node["name"] or r["id"]]))
        if r["kind"] in CODE_TEXT_ITEMS and node["kind"] == "File" and not node["label"].audience:
            text = body(rec)
            if text:
                graph.code[r["id"]] = text
    for nid, first, text, context in long:  # past MAX_TEXT_CHARS, a passage would index what the body cut
        prefix = " > ".join(dict.fromkeys(c for c in context if c))
        found = split_passages(text[: text_search.MAX_TEXT_CHARS], first, tuning.passage_chars)
        graph.passages.setdefault(nid, []).extend({**p, "text": f"{prefix}\n{p['text']}"} for p in found)
    for nid, texts in notes.items():
        if nid in graph.nodes:
            parts[nid] += texts
    return _node_texts(graph, parts)


def _node_texts(graph: Graph, parts: dict[str, list[str]]) -> list[str]:
    """Fill ``graph.paths`` and ``graph.texts`` from ``parts`` and each node's code; the ids of the bodies cut."""
    cut = []
    for nid, n in graph.nodes.items():
        props = n["props"]
        if props.get("code"):
            parts[nid].append(str(props["code"]))
        item = graph.nodes.get(props.get("item")) if n["kind"] == "Section" else None
        path = props.get("path") or (item["props"].get("path") if item else None)
        if path:
            graph.paths[nid] = str(path)
        if parts.get(nid):
            graph.texts[nid], was_cut = text_search.body_text(parts[nid])
            if was_cut:
                cut.append(nid)
    return sorted(cut)


def _section_texts(
    graph: Graph, r: dict, node: dict, frags: list[dict], tuning: Tuning, parts: dict[str, list[str]], long: list
) -> None:
    """Add the text of each fragment of a document to its section's body (the document's, without a section node),
    and to ``long`` a fragment with lines longer than ``passage_from``."""
    heads = {f["anchor"]: f for f in frags}
    for f in frags:
        sid = f"{r['id']}#{f['anchor']}"
        target = sid if sid in graph.nodes else r["id"]
        parts[target].append(f["text"])
        first = _first_line(f.get("span"))
        if first is not None and len(f["text"]) > tuning.passage_from:
            long.append((target, first, f["text"], [node["name"] or r["id"], *_headings(f, heads)]))


def _first_line(span: str | None) -> int | None:
    """The first line of a fragment's ``lines A-B`` span, or None."""
    found = re.match(r"lines? (\d+)", span or "")
    return int(found.group(1)) if found else None


def _headings(frag: dict, heads: dict[str, dict]) -> list[str]:
    """The titles of the headings above a markdown fragment, outermost first, then its own."""
    chain, seen, at = [], set(), frag
    while at is not None and at["anchor"] not in seen:
        seen.add(at["anchor"])
        if at.get("title"):
            chain.append(at["title"])
        at = heads.get(at.get("parent") or "")
    return chain[::-1]


def multiple_definitions(graph: Graph, code_kinds: set[str]) -> dict:
    """Codes with ``defines`` from more than one item. Design 4.6 wants one definition per code, copies
    marked ``derived_from``. A section defines on behalf of its document, so one item counts once."""
    places: dict[str, set[str]] = defaultdict(set)
    for e in graph.edges.values():
        if e["rel"] == "defines" and graph.nodes[e["dst"]]["kind"] in code_kinds:
            src = graph.nodes[e["src"]]
            places[e["dst"]].add(src["props"].get("item", e["src"]) if src["kind"] == "Section" else e["src"])
    codes = sorted(code for code, items in places.items() if len(items) > 1)
    return {
        "codes": len(codes),
        "samples": [{"code": code, "places": sorted(places[code])} for code in codes[:DEFINITION_SAMPLES]],
    }


def is_structured(record: dict) -> bool:
    """Whether the structure stage reads an item: one of STRUCTURED_KINDS, or one that declares nodes or edges."""
    return record["kind"] in STRUCTURED_KINDS or declares(record)


def memo_path(project: Project) -> Path:
    if project.policy.memo == "shared":
        return project.home / "_shared" / "memo.sqlite"
    return project.root / "memo.sqlite"


def build_project(
    project: Project,
    *,
    store: Store | None = None,
    memo: Memo | None = None,
    verify: bool = False,
    cold: bool = False,
    force: bool = False,
    entity_outbox: list | None = None,
    entity_inbox: dict | None = None,
    entity_keys: dict | None = None,
) -> BuildReport:
    """Build the project's graph and append one line to its audit log, whether the build succeeds, fails or
    raises. ``verify`` rebuilds into a temporary store, which adds no line of its own: the outcome is this
    build's ``verified``. A build into the project's own store is skipped, unless ``force``, when its latest build
    was made of the same corpus state with the same inputs (``unchanged``): the report names that build, with
    ``skipped`` set, and the audit log gets no line, since nothing was built. ``entity_outbox``, ``entity_inbox`` and
    ``entity_keys`` go to the entities step (``run_pipeline``)."""
    if store is None and not force:
        report = unchanged(project)
        if report is not None:
            if verify:
                report.verify = verify_build(project, report.content_hash, cold=cold)
                report.ok = report.verify["match"]
            _vectors(project, report)
            return report
    started_at = utc_now()
    report = BuildReport(project.name, ok=False)
    crashed = None
    try:
        warm = _build(project, report, store, memo, entity_outbox, entity_inbox, entity_keys)
        if verify and report.ok:
            report.verify = verify_build(project, report.content_hash, cold=cold, warm=warm)
            report.ok = report.verify["match"]
    except BaseException as exc:
        crashed = type(exc).__name__
        raise
    finally:
        audit.record_build(project.root, report, started=started_at, finished=utc_now(), exception=crashed)
    if store is None and report.ok:
        _vectors(project, report)
    return report


def _semantic_of(tuning):
    """The project's semantic settings (``kg.semantic``), None when they do not read."""
    try:
        return tuning.semantic_settings()
    except ValueError:
        return None


def _vectors(project: Project, report: BuildReport) -> None:
    """Make the project's graph hold the vectors its ``semantic.json`` asks for (``evo_agents.kg.semantic``), outside
    the build's content hash; a model that cannot be loaded is a warning, and the graph searches by words."""
    from evo_agents.kg import semantic

    path = project.root / "graph.sqlite"
    try:
        settings = tuning_of(project).semantic_settings()
    except (ValueError, ProjectError) as exc:
        report.warnings.append(f"semantic: {exc}")
        return
    if not path.exists():
        return
    found = semantic.index_file(path, settings)
    if found.error:
        report.warnings.append(found.line())


def build_inputs(project: Project, corpus: Corpus) -> str | None:
    """Digest of what a build makes of the corpus state besides the state: the code (evo-agents' version, the store
    schema, each stage's version, the graphify it would use, the clustering's version, parameters and engine), the
    project's knowledge.yaml and ontology, and the cursors its coverage copies (``freshness.add_reads``). None when the ontology does not load, which fails a build."""
    from evo_agents.kg.extract import graphify as gfy

    try:
        ontology = project.ontology
    except ProjectError:
        return None
    sources = sorted(s["id"] for s in project.sources() if isinstance(s.get("id"), str))
    parts = {
        "code": [__version__, kg_store.SCHEMA_VERSION, MAP_VERSION, STRUCTURE_VERSION, LINK_VERSION, BINDING_VERSION],
        "graphify": gfy.version() if gfy.available() else None,
        "knowledge": project.knowledge,
        "ontology": ontology,
        "cursors": {sid: corpus.cursor(sid) for sid in sources},
        "clusters": kg_clusters.inputs(),
    }
    tuning = tuning_of(project).overrides()
    chosen = {k: v for k, v in tuning.items() if k in ("passage_from", "passage_chars", "strategy", "entities")}
    if chosen:  # only a project that set them: every other project keeps the digest it had
        parts["tuning"] = chosen
    if "entities" in chosen:  # whether the graph has vectors decides the synonym edges of its entities
        found = _semantic_of(tuning_of(project))
        parts["entity_synonyms"] = [found.model] if found is not None and found.indexes else None
    maps = entity_map.digest(project.root)
    if maps and "entity_map" in strategy.steps_of(
        tuning.get("strategy"), getattr(getattr(project, "harness", None), "root", None)
    ):
        parts["entity_maps"] = maps  # the maps an agent posted (kg.entity_map): a new one is a new build
    return sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def unchanged(project: Project) -> BuildReport | None:
    """The report of the latest build in the project's store (``graph.sqlite``) when it was made of the corpus state
    the project has now, with the inputs it has now (``build_inputs``): a build would write that graph again. None
    otherwise, and when the store is missing, of another schema, or holds no ready build."""
    corpus = project.corpus()
    try:
        state, inputs = corpus.state_digest(), build_inputs(project, corpus)
    finally:
        corpus.close()
    path = project.root / "graph.sqlite"
    if inputs is None or not path.exists():
        return None
    try:
        store = Store.open_file(path)
    except (StaleSchema, sqlite3.Error):
        return None
    try:
        b = store.latest_ready()
        row = store.build_row(b) if b is not None else None
        if row is None or row["state_digest"] != state or row["stats"].get("inputs") != inputs:
            return None
        if entities.unfinished(row["coverage"]):  # items left for a later build: this build goes on with them
            return None
        size = row["stats"].get("size") or store.counts(b)
    finally:
        store.close()
    coverage = row["coverage"]
    report = BuildReport(project.name, ok=True, build_id=b, content_hash=row["content_hash"], skipped=True)
    report.nodes, report.edges, report.units = size["nodes"], size["edges"], size["units"]
    report.coverage, report.warnings = coverage, coverage.get("warnings", [])
    report.issues, report.dangling_edges = coverage.get("issues", []), coverage.get("dangling_edges", 0)
    return report


def _build(
    project: Project,
    report: BuildReport,
    store: Store | None,
    memo: Memo | None,
    entity_outbox: list | None = None,
    entity_inbox: dict | None = None,
    entity_keys: dict | None = None,
) -> Clusters | None:
    """Build into ``store`` (the project's graph.sqlite when None); the clusters the build started from."""
    started = time.monotonic()
    corpus = project.corpus()
    own_memo, own_store = memo is None, store is None
    if memo is None:
        path = memo_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        memo = Memo(path)
    try:
        graph, coverage, errors, issues = run_pipeline(
            project, corpus, memo, entity_outbox=entity_outbox, entity_inbox=entity_inbox, entity_keys=entity_keys
        )
        add_reads(coverage, corpus)
        memo.commit()
        report.memo = memo.stats()
        report.issues = issues[:50]
        report.warnings = coverage.get("warnings", [])
        report.coverage = coverage
        if errors:
            report.errors = errors
            return
        add_curation(graph, coverage)
        target = project.root / "graph.sqlite"
        warm = previous_clusters(store, target)
        add_clusters(project, graph, coverage, warm)
        add_review(graph, coverage)
        if store is None:
            found = Store.stored_schema(target)
            if found not in (None, kg_store.SCHEMA_VERSION):
                report.rebuilt_from = found
            store = Store.staging(target) if report.rebuilt_from else Store(target)
        b = store.write(
            graph,
            state_digest=corpus.read_digest,
            coverage=coverage,
            stats={"memo": report.memo, "inputs": build_inputs(project, corpus)},
            policy=project.policy,
            warm=warm,
        )
        if report.rebuilt_from:
            store = store.install(target)
        counts = store.counts(b)
        report.ok = True
        report.build_id = b
        report.content_hash = store.build_row(b)["content_hash"]
        report.nodes, report.edges, report.units = counts["nodes"], counts["edges"], counts["units"]
        report.dangling_edges = coverage["dangling_edges"]
        return warm
    finally:
        # Since Python 3.11 a connection left open is freed only by the cycle collector (its statement cache refers back
        # to it), and Windows cannot replace a file a connection holds: the next rebuild would fail on graph.sqlite-wal.
        if own_store and store is not None:
            store.close()
        if own_memo:
            memo.close()
        corpus.close()
        report.seconds = round(time.monotonic() - started, 3)


def add_curation(graph: Graph, coverage: dict) -> None:
    """Mark the parts of ``graph`` its deprecations name (``evo_agents.kg.pipeline.curation``) and count them in
    ``coverage["curation"]``; a graph without a deprecation keeps its coverage as it is."""
    if graph.deprecations:
        coverage["curation"] = curation.apply(graph)


def add_clusters(project: Project, graph: Graph, coverage: dict, warm: Clusters | None) -> None:
    """Compute ``graph``'s clusters from ``warm`` when the extra is installed (``evo_agents.kg.clusters``), and say
    in ``coverage["clusters"]`` what came of it. A graph whose clustering fails is built without clusters."""
    if not kg_clusters.available():
        coverage["clusters"] = kg_clusters.missing()
        return
    try:
        graph.clusters = kg_clusters.compute(graph, project.policy, warm)
    except Exception as exc:  # the engine's errors are its own classes; the graph stands without clusters
        coverage["clusters"] = {"computed": False, "reason": f"clustering failed: {type(exc).__name__}: {exc}"}
        return
    coverage["clusters"] = {**kg_clusters.coverage(graph.clusters), "warm": warm is not None}


def add_review(graph: Graph, coverage: dict) -> None:
    """Compute the review items of a graph with clusters (``evo_agents.kg.review``) and count them per rule in
    ``coverage["review"]``; a graph without clusters has none, and its coverage says nothing of them."""
    if graph.clusters is None:
        return
    graph.review = kg_review.compute(graph)
    coverage["review"] = kg_review.counts(graph.review)


def previous_clusters(
    store: Store | None, target: Path, *, before: int | None = None, close: bool = False
) -> Clusters | None:
    """The clusters a build starts from: those of the latest ready build of ``store`` (before build ``before``), or,
    when ``store`` is None or has none and is not ``target`` (a staging store), those of the latest build in
    ``target``. None without one, and for a file of another schema. ``close`` closes ``store``."""
    try:
        if store is not None:
            b = store.latest_ready(before)
            if b is not None:
                return store.clusters(b)
            if before is not None or store.path == target:
                return None
        if not target.exists():
            return None
        try:
            found = Store.open_file(target)
        except (StaleSchema, sqlite3.Error):
            return None
        try:
            b = found.latest_ready()
            return found.clusters(b) if b is not None else None
        finally:
            found.close()
    finally:
        if close and store is not None:
            store.close()


def warm_start_of(project: Project, content_hash: str) -> Clusters | None:
    """The clusters the project's ready build of ``content_hash`` started from: those it recorded (``Store.warm_start``),
    else those of the ready build before it in the project's store. None when the store, that build or the one before
    it is gone."""
    target = project.root / "graph.sqlite"
    if not target.exists():
        return None
    try:
        store = Store.open_file(target)
    except (StaleSchema, sqlite3.Error):
        return None
    try:
        b = store.ready_build_of(content_hash)
    except sqlite3.Error:
        store.close()
        return None
    if b is None:
        store.close()
        return None
    try:
        recorded, warm = store.warm_start(b)
    except sqlite3.Error:  # a file written before cluster_warm came
        recorded, warm = False, None
    if recorded:
        store.close()
        return warm
    return previous_clusters(store, target, before=b, close=True)


_FIND = object()


def verify_build(
    project: Project, expected: str, *, cold: bool = False, warm: Clusters | None | object = _FIND
) -> dict:
    """Rebuild from the corpus into an empty store and compare content hashes (property P1).
    With ``cold`` the memo starts empty too, which also checks that memoized outputs are faithful. ``warm`` is the
    clusters the build started from; by default those of the build before the project's ready build of
    ``expected`` (``warm_start_of``), since cluster ids follow the build before."""
    if warm is _FIND:
        warm = warm_start_of(project, expected)
    tmp = Path(tempfile.mkdtemp(prefix="evo-kg-verify-"))
    opened: list = []  # closed whatever happens: an open memo holds its lock, an open corpus its file
    try:
        store = Store(tmp / "graph.sqlite")
        opened.append(store)
        memo = Memo(None) if cold else Memo(memo_path(project))
        opened.append(memo)
        entity_memo = Memo(memo_path(project)) if cold else None
        if entity_memo is not None:
            opened.append(entity_memo)
        corpus = project.corpus()
        opened.append(corpus)
        graph, coverage, errors, _ = run_pipeline(project, corpus, memo, entity_memo=entity_memo, offline=True)
        if errors:
            return {"match": False, "errors": errors[:10]}
        add_curation(graph, coverage)
        add_clusters(project, graph, coverage, warm)
        add_review(graph, coverage)
        b = store.write(graph, state_digest=corpus.read_digest, coverage=coverage, stats={}, policy=project.policy)
        got = store.build_row(b)["content_hash"]
        return {"match": got == expected, "expected": expected, "rebuilt": got, "cold": cold}
    finally:
        for opener in reversed(opened):
            opener.close()
        shutil.rmtree(tmp, ignore_errors=True)
