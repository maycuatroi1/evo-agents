"""The corpus sets of ``kg bench``: MuSiQue, medical, literature, music and customers, one graph for each corpus.

A *corpus* is the documents of a set built into one graph, and its questions asked on it. Where a memory case is a
haystack for one or a few questions (memory.py), here every question searches the same documents, as a user's
questions search a project's knowledge graph. ``fetch`` downloads the files at the revisions pinned in
``datasets.CORPUS_SOURCES`` into the bench cache, never a repo (``customers`` is made by a seeded generator and
downloads nothing), and reads them into ``corpus.jsonl``, a line a document or a question::

    {"type": "doc", "key", "title", "text", "props": {...}}
    {"type": "question", "id", "question", "kind", "answers": [doc key, ...]}

What each set gives:

- ``musique``: MuSiQue-Ans, dev. A question has 20 paragraphs, 2 to 4 of them supporting; the answers are the
  supporting paragraphs. Only a question with an answer (``answerable``) is read, since a question of ``kg eval``
  needs one; the unanswerable ones are counted and left out. A paragraph two questions share is one document.
- ``medical``: PubMedQA's labelled questions. The document is the abstract (its sections, the MeSH terms as
  metadata); the answer is the abstract the question was asked about.
- ``literature``: NarrativeQA. The document is a story's Wikipedia plot summary with its title and kind (movie or
  book) as metadata; the answer is the story a question was written about.
- ``music``: music-wiki's genres, instruments and artists: the Wikipedia text and the MusicBrainz metadata (type, id,
  url), no audio. A question is made from the first sentence of a page with its name taken out ("Which genre is
  ...?"), the answer is that page.
- ``customers``: fictional customer records from a seeded generator (``generate_customers``): invented names built
  from syllables, invented companies and cities, a plan, a contract note and a support history. A question describes
  one record by its plan, city, seats and a ticket of its history; the answer is that record. No real person or company.

At most MAX_QUESTIONS questions of a set are kept (the ones with the smallest SHA-256 of seed and id), and for
musique only their paragraphs, so a graph stays within a few thousand documents.

Graph. Each document is one item ``corpus:doc:N`` (N from 1, in the order of ``corpus.jsonl``), kind ``document``,
its text the body and its metadata the props, labelled public as the code graphs are. The items reach the build through
the in-process connector of the memory sets. A result is counted for the document its id names (a section of a
document counts for the document), the documents in the order they first come.

Strategy graphs. ``--strategy NAME`` (``GraphConfig``) builds a second graph of the corpus with a kg strategy applied
as ``kg strategy apply`` would (its build keys, ontology and steps, the entities step included), kept apart from the
graph without it (its own repo name, ``<set>/corpus-<strategy>-<digest>``). A strategy whose entities mapping names a
sink gets that sink declared as an ``llm-backend`` that clears public, the level of every item. The query keys of the
strategy (``walk``) are left out of the graph's settings: a run asks them with ``--settings``. ``--entities FILE``
sets the ceilings and ``jobs`` of the mapping, which name no graph; a mapping that names no ``items`` reads the
documents (kind ``document``, which the step's default, the prose kinds, leaves out). A build whose entities step left items for later
(a ceiling, a rate limit) is not marked built, so the next ``build`` goes on from the memo; the total the model's
answers cost (``extraction``) is read from the memo, all builds of the graph together.

Scoring is the memory sets' (``memory.session_metrics`` over documents): ``recall_all@k`` is 1 when every answer
document is among the first k, ``recall_any@k`` when one is, ``ndcg_any@k``; the primary metric is ``recall_all@5``.
A question's part is ``repo_split`` of ``<set>/<question id>``: tune, holdout, test by the hash as the code sets'.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import random
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.kg.bench import datasets, graphs, memory
from evo_agents.kg.bench.datasets import (
    CORPUS_BENCHES,
    CUSTOMERS,
    LITERATURE,
    MEDICAL,
    MUSIC,
    MUSIQUE,
    BenchError,
)
from evo_agents.kg.bench.tasks import Task, repo_split

ITEMS_VERSION = "corpus1"  # the shape of the items: a change builds every corpus graph again
SOURCE_ID = "corpus"
ITEM_KIND = "document"
PRIMARY = memory.PRIMARY
LIMIT = memory.LIMIT
SEED = 20261104  # of the questions kept and of the customers generator
MAX_QUESTIONS = {MUSIQUE: 600, MEDICAL: 1000, LITERATURE: 1000, MUSIC: 800, CUSTOMERS: 800}
CUSTOMER_COUNT = 1500
MAX_FIT = 3  # records a customers question may fit
MAX_CHARS = 6000  # of a document's text
_NODE = re.compile(rf"{SOURCE_ID}:doc:(\d+)")


def is_corpus(bench: str) -> bool:
    return bench in CORPUS_BENCHES


def revision(bench: str) -> dict[str, str]:
    sources = datasets.CORPUS_SOURCES[bench]
    if not sources:
        return {"generator": f"customers-{ITEMS_VERSION}", "seed": str(SEED), "customers": str(CUSTOMER_COUNT)}
    return {f"{s.dataset}:{s.file}" if len(sources) > 1 else s.dataset: s.revision for s in sources}


def question_part(bench: str, question_id: str) -> str:
    return repo_split(f"{bench}/{question_id}")


def _rank(text: str) -> str:
    return hashlib.sha256(f"{SEED}:{text}".encode()).hexdigest()


def _clip(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS].rsplit(" ", 1)[0]


def _key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:12]


def _doc(key: str, title: str, text: str, **props) -> dict:
    return {"type": "doc", "key": key, "title": title, "text": _clip(text), "props": props}


def _question(qid: str, text: str, kind: str, answers: list[str]) -> dict:
    return {"type": "question", "id": qid, "question": text, "kind": kind, "answers": sorted(set(answers))}


def keep_questions(bench: str, questions: list[dict]) -> list[dict]:
    """At most MAX_QUESTIONS[bench] of ``questions``, the ones with the smallest hash of seed and id, in their order."""
    limit = MAX_QUESTIONS[bench]
    if len(questions) <= limit:
        return questions
    chosen = {q["id"] for q in sorted(questions, key=lambda q: _rank(q["id"]))[:limit]}
    return [q for q in questions if q["id"] in chosen]


# -- reading the sets ----------------------------------------------------------------------------------------------


def read_musique(path: Path) -> tuple[list[dict], list[dict], dict]:
    docs: dict[str, dict] = {}
    questions, skipped = [], {}
    for row in datasets.read_rows(path):
        if not row.get("answerable") or not str(row.get("answer") or "").strip():
            datasets._count(skipped, "no answer")
            continue
        found, support = {}, []
        for p in row.get("paragraphs") or []:
            key = _key(str(p.get("title")), str(p.get("paragraph_text")))
            found[key] = _doc(key, str(p.get("title") or ""), str(p.get("paragraph_text") or ""))
            if p.get("is_supporting"):
                support.append(key)
        if not support or not row.get("question"):
            datasets._count(skipped, "no supporting paragraph")
            continue
        qid = str(row["id"])
        hops = qid.split("__", 1)[0].split("hop", 1)[0] + "hop"
        questions.append({**_question(qid, str(row["question"]), hops, support), "_docs": found})
    kept = keep_questions(MUSIQUE, questions)
    for q in kept:
        docs.update(q.pop("_docs"))
    return list(docs.values()), kept, skipped


def read_medical(path: Path) -> tuple[list[dict], list[dict], dict]:
    docs, questions, skipped = [], [], {}
    for row in datasets.read_rows(path):
        pubid, text = str(row.get("pubid") or ""), row.get("question")
        context = row.get("context") or {}
        parts = [str(c).strip() for c in context.get("contexts") or [] if str(c).strip()]
        if not pubid or not text or not parts:
            datasets._count(skipped, "no question or abstract")
            continue
        labels = [str(x) for x in context.get("labels") or []]
        body = "\n\n".join(
            f"{lab.title()}: {c}" if lab else c for lab, c in zip(labels + [""] * len(parts), parts, strict=False)
        )
        meshes = [str(m) for m in context.get("meshes") or []]
        if meshes:
            body += "\n\nMeSH terms: " + ", ".join(meshes)
        docs.append(_doc(pubid, f"PMID {pubid}", body, pubid=pubid, mesh=len(meshes)))
        questions.append(_question(pubid, str(text).strip(), str(row.get("final_decision") or "other"), [pubid]))
    return docs, keep_questions(MEDICAL, questions), skipped


def read_literature(path: Path) -> tuple[list[dict], list[dict], dict]:
    import csv

    def table(zf: zipfile.ZipFile, name: str) -> list[dict]:
        with zf.open(f"narrativeqa-master/{name}") as f:
            return list(csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline="")))

    with zipfile.ZipFile(path) as zf:
        meta = {r["document_id"]: r for r in table(zf, "documents.csv")}
        summaries = {r["document_id"]: r for r in table(zf, "third_party/wikipedia/summaries.csv")}
        qaps = table(zf, "qaps.csv")
    docs, questions, skipped = [], [], {}
    for did, row in sorted(summaries.items()):
        text = (row.get("summary") or "").strip()
        info = meta.get(did) or {}
        if not text:
            datasets._count(skipped, "no summary")
            continue
        title = (info.get("wiki_title") or did).strip()
        docs.append(
            _doc(
                did, title, text, kind=info.get("kind") or "story", wiki=info.get("wiki_url") or "", set=row.get("set")
            )
        )
    have = {d["key"] for d in docs}
    seen: dict[str, int] = {}
    for r in qaps:
        did = r["document_id"]
        if did not in have or not r.get("question", "").strip() or not r.get("answer1", "").strip():
            datasets._count(skipped, "no summary or answer")
            continue
        n = seen[did] = seen.get(did, -1) + 1
        questions.append(_question(f"{did[:12]}.{n}", r["question"].strip(), meta[did].get("kind") or "story", [did]))
    return docs, keep_questions(LITERATURE, questions), skipped


def first_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    found = re.search(r"(?<=[a-z0-9\)\"'])[.!?](?=\s+[A-Z]|\s*$)", text)
    return text[: found.end()] if found else text[:300]


def mask(sentence: str, name: str) -> str:
    """``sentence`` with ``name`` (and its words when long) taken out, each place becoming ``this``."""
    out = re.sub(re.escape(name), "this", sentence, flags=re.IGNORECASE)
    for word in re.findall(r"\w{5,}", name):
        out = re.sub(rf"\b{re.escape(word)}\b", "this", out, flags=re.IGNORECASE)
    return out


MUSIC_KINDS = ("genre", "instrument", "artist")
MUSIC_ARTISTS = 1500  # of the 7002 artist pages: the ones with the smallest hash


def read_music(paths: dict[str, Path]) -> tuple[list[dict], list[dict], dict]:
    docs, questions, skipped = [], [], {}
    for file, path in paths.items():
        kind = next(k for k in MUSIC_KINDS if f"musicbrainz_{k}-" in file)
        rows = datasets.read_rows(path)
        if kind == "artist":
            rows = sorted(rows, key=lambda r: _rank(str(r.get("mbid"))))[:MUSIC_ARTISTS]
        for row in rows:
            name, text, mbid = (
                str(row.get("entity") or "").strip(),
                str(row.get("text") or "").strip(),
                str(row.get("mbid") or ""),
            )
            if not name or not mbid or len(text) < 80:
                datasets._count(skipped, "no name or text")
                continue
            docs.append(_doc(mbid, name, text, entity_type=kind, mbid=mbid, url=row.get("url") or ""))
            clue = mask(first_sentence(text), name)
            if len(clue) < 50 or clue.count("this") > 3:
                datasets._count(skipped, "no usable first sentence")
                continue
            questions.append(_question(mbid, f"Which {kind} page says: {clue}", kind, [mbid]))
    return docs, keep_questions(MUSIC, questions), skipped


# -- customers: a seeded generator, invented names ------------------------------------------------------------------

_ONSET = [
    "b",
    "br",
    "c",
    "d",
    "dr",
    "f",
    "g",
    "gl",
    "h",
    "k",
    "kl",
    "l",
    "m",
    "n",
    "p",
    "pr",
    "r",
    "s",
    "st",
    "t",
    "v",
    "z",
]
_VOWEL = ["a", "e", "i", "o", "u", "ae", "io", "ou"]
_CODA = ["", "l", "n", "r", "s", "th", "x", "rn", "lk", "m"]
_PLANS = ["Starter", "Team", "Business", "Enterprise"]
_TOPICS = [
    "a billing error on the invoice",
    "a failed single sign-on login",
    "a slow export of the monthly report",
    "a request to move the data to another region",
    "a missing seat after the renewal",
    "a webhook that stopped firing",
    "a question about the retention period",
    "a request for a second admin account",
]
_NOTES = [
    "renews every spring and asks for a discount",
    "pays by purchase order, net 60",
    "has a named contact for incidents",
    "runs a pilot with two departments",
    "asked for a custom data processing addendum",
    "left a long review after the onboarding call",
]


def _word(rng: random.Random, syllables: tuple[int, int] = (2, 3)) -> str:
    n = rng.randint(*syllables)
    return "".join(
        rng.choice(_ONSET) + rng.choice(_VOWEL) + (rng.choice(_CODA) if i == n - 1 else "") for i in range(n)
    ).title()


def _customer_question(r: dict, style: str, extra: str) -> tuple[str, dict]:
    """The text of a question about record ``r`` and the facts it states (``_fits``)."""
    where = {"plan": r["plan"], "city": r["city"]}
    if extra:
        where["seats"] = r["seats"]
    if style == "ticket":
        topic, ref = r["tickets"][0]
        about = f"{topic} {ref}" if ref else topic
        return (
            f"Which {r['plan']} plan customer in {r['city']}{extra} contacted support about {about}?",
            {**where, "ticket": (topic, ref)},
        )
    if style == "note":
        return f"Which {r['plan']} plan customer in {r['city']}{extra} {r['note']}?", {**where, "note": r["note"]}
    return f"Which customer of {r['company']} has {r['seats']} seats?", {"company": r["company"], "seats": r["seats"]}


def _fits(record: dict, facts: dict) -> bool:
    return all(facts[k] in record["tickets"] if k == "ticket" else record[k] == facts[k] for k in facts)


def generate_customers(count: int = CUSTOMER_COUNT, seed: int = SEED) -> tuple[list[dict], list[dict]]:
    """``count`` fictional customer records and a question for each, from ``seed``: names are built from syllables
    (Drokaeth Pelinor, Gloxen Vorm), places from the same, so none is a real person, company or town. Two records
    never share a name. A question describes a record by some of its facts, in one of three styles (plan, city and a
    ticket; plan, city and the account note; company and seats); its answers are every record those facts fit, and a
    question that fits more than MAX_FIT records is made again with the seats added, so the answer stays findable."""
    rng = random.Random(seed)
    cities = [_word(rng, (2, 3)) + rng.choice(["ford", "port", "vale", "mere", "stad"]) for _ in range(40)]
    records, names = [], set()
    for number in range(1, count + 1):
        while True:
            name = f"{_word(rng)} {_word(rng)}"
            if name not in names:
                names.add(name)
                break
        invoice = rng.randint(10000, 99999)
        tickets = []
        for _ in range(rng.randint(1, 3)):
            topic = rng.choice(_TOPICS)
            tickets.append((topic, invoice + rng.randint(1, 900) if "invoice" in topic else None))
        records.append(
            {
                "id": f"C-{number:04d}",
                "name": name,
                "company": f"{_word(rng, (2, 2))} {rng.choice(['Works', 'Labs', 'Freight', 'Foods', 'Media', 'Systems'])}",
                "city": rng.choice(cities),
                "plan": rng.choice(_PLANS),
                "note": rng.choice(_NOTES),
                "seats": rng.randint(3, 400),
                "tickets": tickets,
            }
        )
    docs = []
    for r in records:
        history = "\n".join(f"- Ticket: {t}{f' {n}' if n else ''}." for t, n in r["tickets"])
        text = (
            f"Customer {r['name']} of {r['company']} in {r['city']}.\nPlan: {r['plan']}, {r['seats']} seats.\n"
            f"Account note: this customer {r['note']}.\nSupport history:\n{history}"
        )
        docs.append(
            _doc(r["id"], f"customers/{r['id']}.md {r['name']}", text, customer=r["id"], plan=r["plan"], city=r["city"])
        )
    questions = []
    for r in records:
        style = rng.choice(("ticket", "note", "seats"))
        for extra in ("", f" with {r['seats']} seats"):
            text, facts = _customer_question(r, style, extra)
            answers = [o["id"] for o in records if _fits(o, facts)]
            if len(answers) <= MAX_FIT or extra or style == "seats":
                break
        questions.append(_question(r["id"], text, style, answers))
    return docs, questions


def read_customers() -> tuple[list[dict], list[dict], dict]:
    docs, questions = generate_customers()
    return docs, keep_questions(CUSTOMERS, questions), {}


# -- fetch and load ------------------------------------------------------------------------------------------------


def fetch(bench: str, cache: Path, *, mirror: Path | None = None, force: bool = False) -> dict:
    """Download the files of ``bench`` at their pinned revisions, read them into corpus.jsonl and write fetched.json,
    which it returns (``datasets.fetch`` for a corpus set)."""
    files, paths = [], {}
    for source in datasets.CORPUS_SOURCES[bench]:
        target = datasets.raw_path(cache, bench, source)
        if force or not target.is_file():
            datasets._download(source, target, mirror)
        paths[source.file] = target
        files.append(
            {
                "dataset": source.dataset,
                "revision": source.revision,
                "file": source.file,
                "bytes": target.stat().st_size,
                "sha256": datasets._sha256(target),
            }
        )
    first = next(iter(paths.values()), None)
    if bench == MUSIQUE:
        docs, questions, skipped = read_musique(first)
    elif bench == MEDICAL:
        docs, questions, skipped = read_medical(first)
    elif bench == LITERATURE:
        docs, questions, skipped = read_literature(first)
    elif bench == MUSIC:
        docs, questions, skipped = read_music(paths)
    else:
        docs, questions, skipped = read_customers()
    keys = {d["key"] for d in docs}
    questions = [q for q in questions if q["answers"] and set(q["answers"]) <= keys]
    out = datasets.data_dir(cache, bench)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "corpus.jsonl.tmp").open("w", encoding="utf-8") as f:
        for line in [*docs, *questions]:
            f.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(out / "corpus.jsonl.tmp", out / "corpus.jsonl")
    fetched = {
        "bench": bench,
        "files": files,
        "tasks": len(questions),
        "documents": len(docs),
        "license": datasets.LICENSES[bench],
        "skipped": skipped,
    }
    (out / "fetched.json").write_text(json.dumps(fetched, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return fetched


def load(cache: Path, bench: str) -> tuple[list[dict], list[dict]]:
    """The documents and questions of a fetched set, in file order."""
    path = datasets.data_dir(cache, bench) / "corpus.jsonl"
    docs, questions = [], []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    (docs if row["type"] == "doc" else questions).append(row)
    except FileNotFoundError:
        raise BenchError(f"{bench} is not fetched: run `evo-agents kg bench fetch {bench}`") from None
    return docs, questions


# -- the graph -----------------------------------------------------------------------------------------------------


def digest(docs: list[dict]) -> str:
    """Names the documents: a graph is rebuilt when they change."""
    h = hashlib.sha256()
    for d in docs:
        h.update(json.dumps([d["key"], d["title"], d["text"], d["props"]], sort_keys=True).encode())
    return h.hexdigest()[:10]


RUN_KEYS = ("jobs", "max_calls", "max_tokens", "max_cost")  # of an entities mapping: they name no graph


@dataclass(frozen=True)
class GraphConfig:
    """A corpus graph built with the strategy ``strategy`` applied; ``entities`` holds the keys of RUN_KEYS set on its
    entities mapping (``--entities``)."""

    strategy: str
    entities: dict = field(default_factory=dict, hash=False, compare=False)

    def found(self):
        from evo_agents.kg import strategy as kg_strategy

        try:
            return kg_strategy.find(self.strategy)
        except kg_strategy.StrategyError as exc:
            raise BenchError(f"--strategy {self.strategy}: {exc}") from None

    def digest(self) -> str:
        """Names what the graph is built with: the strategy's settings but the query keys and RUN_KEYS, its ontology
        and steps."""
        from evo_agents.kg import settings as kg_settings

        chosen = self.found()
        values = {k: v for k, v in chosen.settings.items() if k in kg_settings.BUILD_KEYS | kg_settings.SEMANTIC_KEYS}
        if isinstance(values.get("entities"), dict):
            values["entities"] = {k: v for k, v in values["entities"].items() if k not in RUN_KEYS}
        data = {"settings": values, "ontology": chosen.ontology, "steps": list(chosen.steps)}
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:8]


def check_entities(data: dict) -> dict:
    unknown = sorted(set(data) - set(RUN_KEYS))
    if unknown:
        raise BenchError(f"--entities sets only {', '.join(RUN_KEYS)}, not {', '.join(unknown)}")
    return data


def stand_in(bench: str, docs: list[dict], graph: GraphConfig | None = None) -> Task:
    """The corpus as the Task the graph cache names, sizes and evicts graphs by (``graphs.Layout``)."""
    return Task(
        bench=bench,
        id="corpus",
        key=bench,
        repo=f"{bench}/corpus" + (f"-{graph.found().name}-{graph.digest()}" if graph else ""),
        commit=f"{ITEMS_VERSION}-{digest(docs)}",
        language="document",
        question="",
    )


def items(docs: list[dict]) -> list[dict]:
    label = {"level": "public", "integrity": "U"}
    out = []
    for n, d in enumerate(docs, 1):
        out.append(
            {
                "id": f"{SOURCE_ID}:doc:{n}",
                "kind": ITEM_KIND,
                "title": d["title"],
                "rev_time": "2026-01-01T00:00:00Z",
                "props": d["props"],
                "label": label,
                "body": {"format": "markdown", "text": d["text"]},
            }
        )
    return out


def write_harness(lay: graphs.Layout, task: Task, docs: list[dict], graph: GraphConfig | None = None) -> Path:
    root = lay.harness(task)
    root.mkdir(parents=True, exist_ok=True)
    name = lay.name(task)
    with (root / "items.jsonl").open("w", encoding="utf-8") as f:
        for item in items(docs):
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    (root / "harness.yaml").write_text(f"name: {name}\nrepos: []\nknowledge_file: knowledge.yaml\n", encoding="utf-8")
    knowledge = {
        "version": 1,
        "project": name,
        "policy": {
            "levels": ["public"],
            "sinks": [{"id": "bench", "kind": "agent-session", "clearance": {"level": "public"}}],
        },
        "sources": [
            {
                "id": SOURCE_ID,
                "connector": f"python:{memory.__name__}:connector",
                "config": {"file": str(root / "items.jsonl")},
                "label": {"level": "public", "integrity": "U"},
            }
        ],
    }
    if graph is not None:
        sink = (graph.found().settings.get("entities") or {}).get("sink")
        if sink:
            knowledge["policy"]["sinks"].append({"id": sink, "kind": "llm-backend", "clearance": {"level": "public"}})
    if graph is None:
        (root / "knowledge.yaml").write_text(json.dumps(knowledge, indent=1) + "\n", encoding="utf-8")
    else:  # YAML: applying the strategy appends its keys as YAML text
        import yaml

        (root / "knowledge.yaml").write_text(yaml.safe_dump(knowledge, sort_keys=False), encoding="utf-8")
        apply_strategy(lay, task, graph)
    return root


def apply_strategy(lay: graphs.Layout, task: Task, graph: GraphConfig) -> None:
    """Apply the strategy to the corpus project, as ``kg strategy apply`` does, then take its query keys out of the
    project's settings and set the RUN_KEYS of its entities mapping."""
    from evo_agents.kg import settings as kg_settings
    from evo_agents.kg import strategy as kg_strategy

    chosen = graph.found()
    if chosen.origin != "built-in":  # a strategy file: the project finds its steps by name in its strategies
        mine = lay.harness(task) / kg_strategy.CUSTOM_DIR
        mine.mkdir(parents=True, exist_ok=True)
        (mine / f"{chosen.name}.yaml").write_text(Path(chosen.origin).read_text(encoding="utf-8"), encoding="utf-8")
    with graphs.bench_home(lay.home):
        project = graphs.project_of(lay, task)
        try:
            kg_strategy.apply(chosen, project, reason="kg bench --strategy")
        except kg_strategy.StrategyError as exc:
            raise BenchError(f"--strategy {graph.strategy}: {exc}") from None
        values = kg_settings.read(project.root).overrides()
        values = {k: v for k, v in values.items() if k in kg_settings.BUILD_KEYS | kg_settings.SEMANTIC_KEYS}
        if isinstance(values.get("entities"), dict):
            # the items are of kind document, which the step's default (the prose kinds) leaves out
            values["entities"] = {"items": [ITEM_KIND], **values["entities"], **graph.entities}
        kg_settings.write(project.root, kg_settings.of(values), reason="kg bench: the build keys of the strategy")


def build_corpus(
    lay: graphs.Layout,
    bench: str,
    docs: list[dict],
    *,
    room: graphs.Room = graphs.DEFAULT_ROOM,
    graph: GraphConfig | None = None,
) -> graphs.Built:
    """The graph of a corpus (with ``graph``, the one of that strategy): reused when built with this extractor and
    these documents."""
    task = stand_in(bench, docs, graph)
    return memory.build_stand_in(
        lay,
        task,
        lambda: write_harness(lay, task, docs, graph),
        room=room,
        keep=frozenset({lay.name(task)}),
        check=_entities_finished if graph is not None else None,
    )


def _entities_finished(report) -> str | None:
    """Why a build of a strategy graph is not the graph to ask: its entities step left items for a later build."""
    from evo_agents.kg import entities

    found = report.coverage.get("entities") or {}
    if not entities.unfinished(report.coverage):
        return None
    why = found.get("stopped") or f"{found.get('failed', 0)} item(s) failed"
    return (
        f"entities unfinished: {found.get('skipped', 0)} skipped, {found.get('failed', 0)} failed, "
        f"{found.get('calls', 0)} calls this build ({why}); build again to go on from the memo"
    )


def extraction(lay: graphs.Layout, task: Task, documents: int) -> dict | None:
    """What the entities step's answers in the graph's memo cost, all builds together: answers, tokens, USD, and USD
    per 1,000 documents of the corpus. None for a graph without the step."""
    import gzip
    import sqlite3

    from evo_agents.kg import settings as kg_settings
    from evo_agents.kg.build import memo_path

    with graphs.bench_home(lay.home):
        project = graphs.project_of(lay, task)
        path = memo_path(project)
        settings = kg_settings.read(project.root).entities
    if not settings or not path.is_file():
        return None
    answers = tokens_in = tokens_out = 0
    usd = 0.0
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for (blob,) in db.execute("SELECT output FROM memo WHERE stage = 'entities'"):
            usage = (json.loads(gzip.decompress(blob)) or {}).get("usage") or {}
            answers += 1
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
            if usage.get("cost") is not None:
                usd += float(usage["cost"])
            else:
                usd += _priced(usage, settings.get("price") or {})
    finally:
        db.close()
    return {
        "model": settings.get("model"),
        "documents": documents,
        "answers": answers,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "usd": round(usd, 4),
        "usd_per_1000_documents": round(usd / documents * 1000, 4) if documents else None,
        "priced_by": "the cost Claude Code reported, else the mapping's price",
    }


def _priced(usage: dict, price: dict) -> float:
    """The USD of a call that reported no cost, by the mapping's ``price`` per million tokens."""
    tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
    return tin / 1e6 * float(price.get("input", 0)) + tout / 1e6 * float(price.get("output", 0))


# -- scoring -------------------------------------------------------------------------------------------------------


def doc_of_node(node_id: str) -> int | None:
    """The 1-based number of the document a result is (or is a section of)."""
    found = _NODE.search(node_id or "")
    return int(found.group(1)) if found else None


def ranked_docs(nodes: list[dict]) -> list[int]:
    out: list[int] = []
    for node in nodes:
        n = doc_of_node(str(node.get("id") or ""))
        if n is not None and n not in out:
            out.append(n)
    return out


def _record(bench: str, q: dict, part: str, metrics: dict, tokens=0, ms=0.0, error=None, skipped=None) -> dict:
    return {
        "bench": bench,
        "id": q["id"],
        "part": part,
        "repo": f"{bench}/corpus",
        "language": q["kind"],
        "dataset": bench,
        "method": "kg_search",
        "metrics": metrics,
        "tokens": tokens,
        "ms": round(ms, 1),
        "error": error,
        "skipped": skipped,
    }


def select(questions: list[dict], bench: str, options: memory.Options) -> tuple[list[dict], dict | None]:
    """The questions in the parts asked for, sampled by the seed when ``options.sample`` is less than they are."""
    mine = [q for q in questions if question_part(bench, q["id"]) in options.parts]
    size = options.sample
    if size is None or size >= len(mine):
        return mine, None
    order = sorted(mine, key=lambda q: hashlib.sha256(f"{options.seed}:{bench}:{q['id']}".encode()).hexdigest())
    keep = {q["id"] for q in order[:size]}
    chosen = [q for q in mine if q["id"] in keep]
    return chosen, {
        "size": len(chosen),
        "of": len(mine),
        "seed": options.seed,
        "why": f"--sample {size}",
        "ids": [q["id"] for q in chosen],
    }


def run_corpus(
    cache: Path, bench: str, options: memory.Options, *, search: str = "graph", graph: GraphConfig | None = None
) -> tuple[dict, list[dict]]:
    """Build the corpus' graph (``graph``'s, with one) and ask the selected questions, ranked as ``search`` says
    (graph or hybrid): the set's part of the summary and its task records."""
    if search not in ("graph", "hybrid"):
        raise BenchError(f"{bench} is asked with --search graph or hybrid, not {search}")
    docs, questions = load(cache, bench)
    asked, picked = select(questions, bench, options)
    lay = graphs.layout(cache)
    task = stand_in(bench, docs, graph)
    say = options.progress or (lambda line: None)
    built = build_corpus(lay, bench, docs, room=options.room, graph=graph)
    say(f"{bench}: {len(docs)} documents, graph " + ("reused" if built.reused else built.error or f"{built.seconds}s"))
    records: list[dict] = []
    number = {d["key"]: n for n, d in enumerate(docs, 1)}
    if built.error:
        records = [
            _record(bench, q, question_part(bench, q["id"]), {}, skipped=f"no graph: {built.error[:200]}")
            for q in asked
        ]
    elif asked:
        from evo_agents.kg.bench.run import _ensure_vectors, session_of

        if search == "hybrid":
            _ensure_vectors(lay.project_dir(task), lay.vectors(task.repo))
        graphs.warm(lay.project_dir(task))
        with graphs.bench_home(lay.home):
            session = session_of(graphs.project_of(lay, task), search)
            for q in asked:
                nodes, tokens, ms, error = memory.ask(session, q["question"], options.limit)
                answers = sorted(number[k] for k in q["answers"])
                metrics = memory.session_metrics(ranked_docs(nodes), answers)
                records.append(_record(bench, q, question_part(bench, q["id"]), metrics, tokens, ms, error))
    part = {
        "kind": "corpus",
        "revision": revision(bench),
        "license": datasets.LICENSES[bench],
        "fetched": [
            {k: f[k] for k in ("dataset", "file", "sha256")}
            for f in (datasets.fetched_info(cache, bench) or {}).get("files", [])
        ],
        "primary": PRIMARY,
        "method": "kg_search",
        "limit": options.limit,
        "granularity": "document",
        "tasks": sum(1 for q in questions if question_part(bench, q["id"]) in options.parts),
        "deduped": 0,
        "asked": len(records),
        "sample": picked,
        "documents": len(docs),
        **({"graph": {"strategy": graph.found().name, "digest": graph.digest(), "repo": task.repo}} if graph else {}),
        **({"extraction": extraction(lay, task, len(docs))} if graph and not built.error else {}),
        "build": {
            "graphs": 1,
            "built": int(not built.reused and not built.error),
            "reused": int(built.reused),
            "failed": int(bool(built.error)),
            "seconds": round(built.seconds, 1),
        },
        "parts": {p: memory.part_numbers([r for r in records if r["part"] == p]) for p in options.parts},
    }
    return part, records


def build_only(cache: Path, bench: str, options: memory.Options, graph: GraphConfig | None = None) -> graphs.Built:
    docs, _ = load(cache, bench)
    return build_corpus(graphs.layout(cache), bench, docs, room=options.room, graph=graph)
