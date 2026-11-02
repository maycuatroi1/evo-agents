"""The memory benches of ``kg bench``: LongMemEval (its cleaned S variant) and MemoryAgentBench, as chats in a graph.

A *case* is one haystack, built into one graph: a LongMemEval question's ~50 sessions, or one context of
MemoryAgentBench with the questions asked on it. ``fetch`` downloads the files at the revisions pinned here
(``SOURCES``, both sets MIT) into the bench cache, never a repo, and reads them into ``cases.jsonl``, one case a line::

    {"bench", "id", "dataset", "ability", "sessions": [{"date", "turns": [{"role", "content"}]}],
     "questions": [{"id", "question", "type", "answers": [session index, ...] | null, "unmatched"}]}

Graph. Each session becomes a chat of the hub's graph, the way the hub's chats go into it
(``evo_agents.hub.server.chat_graph``, which this package does not import): a Chat item, one ChatThread item rooted
at its first turn, and a ChatMessage item per turn, named ``#<id> <author> <time>``, its text the turn's content and
its props ``chat``, ``thread``, ``seq``, ``author_kind`` (person for the user, agent for the assistant), ``author`` and
``sent_at``. A context of MemoryAgentBench that is not a list of sessions (documents, a novel, a list of facts) is one
chat whose messages are its chunks of at most CHUNK_CHARS, cut at a blank line, else a line end. The items reach the
build through an in-process connector (``connector``) reading the case's items file, labelled public as the code
graphs are, without an audience.

Scoring, as LongMemEval's ``print_retrieval_metrics.py`` does: a question whose id has ``_abs`` (abstention) is left
out; kg_search is asked the question verbatim with its largest limit (LIMIT), each result counted for the session
its chat, thread or message is in, the sessions in the order they first come; ``recall_all@k`` is 1 when every answer
session is among the first k, ``recall_any@k`` when one is, ``ndcg_any@k`` as the set's ``eval_utils.ndcg``. The
answer sessions are LongMemEval's ``answer_session_ids``, and in MemoryAgentBench's ``longmemeval_s*`` contexts the
sessions its ``haystack_sessions`` name for each question, matched to the context's sessions by their turns (one
that matches none stays an answer no ranking finds, counted as ``unmatched``). The other datasets of
MemoryAgentBench give no evidence to score retrieval by: their graphs are built and the summary says
``no retrieval labels`` for them, rather than labels made up here.

Parts. A question's part is ``repo_split`` of ``longmemeval/<question id>`` (MemoryAgentBench's ``longmemeval_s*``
questions are LongMemEval's, and fall in the same part), a context's unscored questions take its case's part. A case
is built when one of its scored questions is in a part asked for; a case without labels is always built, since nothing
of it is scored.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.kg.bench import datasets, graphs
from evo_agents.kg.bench.datasets import BenchError, Source
from evo_agents.kg.bench.tasks import Task, repo_split
from evo_agents.kg.evaluate import estimate_tokens, p95

LONGMEMEVAL = "longmemeval"
MEMORYAGENTBENCH = "memoryagentbench"
MEMORY_BENCHES = (LONGMEMEVAL, MEMORYAGENTBENCH)
MAB_ACCURATE = "accurate_retrieval"
MAB_FORGETTING = "conflict_resolution"  # selective forgetting: FactConsolidation
MAB_REVISION = "7ea066982b140a19337e17e60d45d4076e042faf"
SOURCES: dict[str, tuple[Source, ...]] = {
    LONGMEMEVAL: (
        Source(
            "xiaowu0162/longmemeval-cleaned", "98d7416c24c778c2fee6e6f3006e7a073259d48f", "longmemeval_s_cleaned.json"
        ),
    ),
    MEMORYAGENTBENCH: (
        Source("ai-hyz/MemoryAgentBench", MAB_REVISION, "data/Accurate_Retrieval-00000-of-00001.parquet"),
        Source("ai-hyz/MemoryAgentBench", MAB_REVISION, "data/Conflict_Resolution-00000-of-00001.parquet"),
    ),
}
MAB_ABILITY = {SOURCES[MEMORYAGENTBENCH][0].file: MAB_ACCURATE, SOURCES[MEMORYAGENTBENCH][1].file: MAB_FORGETTING}
# LongMemEval's question types by the ability the paper groups them under; abstention (`_abs` ids) is not scored
ABILITIES = {
    "single-session-user": "information_extraction",
    "single-session-assistant": "information_extraction",
    "single-session-preference": "information_extraction",
    "multi-session": "multi_session_reasoning",
    "knowledge-update": "knowledge_update",
    "temporal-reasoning": "temporal_reasoning",
}
ITEMS_VERSION = "items1"  # the shape of the items: a change builds every memory graph again
SOURCE_ID = "mem"
CHUNK_CHARS = 2048  # a chunk of a context that is not sessions: about 512 tokens
LIMIT = 25  # kg_search's largest limit
KS = (5, 10)
PRIMARY = "recall_all@5"
METRICS = tuple(f"{m}@{k}" for k in KS for m in ("recall_all", "recall_any", "ndcg_any"))
NO_LABELS = "no retrieval labels"
LONGMEMEVAL_DATASET = "longmemeval_s"
CHAT_TIME = re.compile(r"^\s*Chat Time:\s*")
DATE = re.compile(r"(\d{4})/(\d{2})/(\d{2})(?:\s*\(\w+\))?\s*(\d{2}):(\d{2})")


def is_memory(bench: str) -> bool:
    return bench in MEMORY_BENCHES


def revision(bench: str) -> dict[str, str]:
    return {s.dataset: s.revision for s in SOURCES[bench]}


def is_abstention(question_id: str) -> bool:
    return "_abs" in question_id


def question_part(question_id: str) -> str:
    """The part of a question (see the module), the same in both sets."""
    return repo_split(f"longmemeval/{question_id}")


# -- reading the sets ----------------------------------------------------------------------------------------------


def _turns(session) -> list[dict]:
    return [{"role": str(t.get("role") or "user"), "content": str(t.get("content") or "")} for t in session or []]


def _turn_key(session) -> tuple:
    return tuple((str(t.get("role") or "user"), str(t.get("content") or "")) for t in session or [])


def read_longmemeval(path: Path) -> tuple[list[dict], dict]:
    """A case per question of LongMemEval S: its haystack, its one question, the indices of its answer sessions."""
    cases, skipped = [], {}
    for row in datasets.read_rows(path):
        qid = str(row.get("question_id") or "")
        ids = list(row.get("haystack_session_ids") or [])
        if not qid or not ids or not row.get("question"):
            datasets._count(skipped, "no question or haystack")
            continue
        dates = list(row.get("haystack_dates") or [None] * len(ids))
        sessions = [
            {"date": d, "turns": _turns(s)} for d, s in zip(dates, row.get("haystack_sessions") or [], strict=False)
        ]
        index = {sid: i for i, sid in enumerate(ids)}
        wanted = list(row.get("answer_session_ids") or [])
        answers = sorted({index[s] for s in wanted if s in index})
        question = {
            "id": qid,
            "question": row["question"],
            "type": row.get("question_type") or "other",
            "date": row.get("question_date"),
            "answers": answers,
            "unmatched": sum(1 for s in wanted if s not in index),
        }
        cases.append(
            {
                "bench": LONGMEMEVAL,
                "id": qid,
                "dataset": LONGMEMEVAL_DATASET,
                "ability": None,
                "sessions": sessions,
                "questions": [question],
            }
        )
    return cases, skipped


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """``text`` cut into pieces of at most ``size`` characters, at a blank line where one is near, else a line end,
    else anywhere; no piece is blank."""
    out, rest = [], text
    while rest:
        if len(rest) <= size:
            piece, rest = rest, ""
        else:
            window = rest[:size]
            cut = window.rfind("\n\n")
            if cut < size // 4:
                cut = window.rfind("\n")
            if cut < size // 4:
                cut = size
            piece, rest = rest[:cut], rest[cut:]
        if piece.strip():
            out.append(piece.strip("\n"))
    return out


def context_sessions(context: str) -> list[dict] | None:
    """The sessions of a MemoryAgentBench context that lists them (``['Chat Time: ...', [turns], ...]``), else None."""
    if not context.lstrip().startswith("["):
        return None
    try:
        value = ast.literal_eval(context)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    if not isinstance(value, list) or len(value) % 2:
        return None
    sessions = []
    for head, turns in zip(value[::2], value[1::2], strict=True):
        if not isinstance(head, str) or not isinstance(turns, list):
            return None
        sessions.append({"date": CHAT_TIME.sub("", head).strip() or None, "turns": _turns(turns)})
    return sessions


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", text.lower()).strip("-") or "x"


def read_memoryagentbench(paths: dict[str, Path]) -> tuple[list[dict], dict]:
    """A case per context of the accurate retrieval and selective forgetting files (``paths``: file -> path)."""
    cases, skipped = [], {}
    for file, path in paths.items():
        ability = MAB_ABILITY[file]
        seen: dict[str, int] = {}
        for row in datasets.read_rows(path):
            meta = row.get("metadata") or {}
            dataset = str(meta.get("source") or "unknown")
            number = seen[dataset] = seen.get(dataset, -1) + 1
            context = str(row.get("context") or "")
            asked = list(row.get("questions") or [])
            if not context.strip() or not asked:
                datasets._count(skipped, "no context or question")
                continue
            sessions = context_sessions(context)
            evidence = meta.get("haystack_sessions")
            labelled = sessions is not None and isinstance(evidence, list) and len(evidence) == len(asked)
            if sessions is None:
                sessions = [{"date": None, "turns": [{"role": "user", "content": c} for c in chunks(context)]}]
            index: dict[tuple, int] = {}
            for i, s in enumerate(sessions):
                index.setdefault(tuple((t["role"], t["content"]) for t in s["turns"]), i)
            ids = list(meta.get("question_ids") or meta.get("qa_pair_ids") or [])
            types = list(meta.get("question_types") or [])
            questions = []
            for n, text in enumerate(asked):
                qid = str(ids[n]) if n < len(ids) and ids[n] else f"{dataset}_no{n}"
                question = {
                    "id": qid,
                    "question": str(text),
                    "type": types[n] if n < len(types) and types[n] else dataset,
                    "answers": None,
                    "unmatched": 0,
                }
                if labelled:
                    keys = [_turn_key(s) for s in evidence[n] or []]
                    question["answers"] = sorted({index[k] for k in keys if k in index})
                    question["unmatched"] = sum(1 for k in keys if k not in index)
                questions.append(question)
            cases.append(
                {
                    "bench": MEMORYAGENTBENCH,
                    "id": f"{_slug(dataset)}.{number}",
                    "dataset": dataset,
                    "ability": ability,
                    "sessions": sessions,
                    "questions": questions,
                }
            )
    return cases, skipped


def fetch(bench: str, cache: Path, *, mirror: Path | None = None, force: bool = False) -> dict:
    """Download the files of ``bench`` at their pinned revisions, read them into cases.jsonl and write fetched.json,
    which it returns (``datasets.fetch`` for a memory set)."""
    files, paths = [], {}
    for source in SOURCES[bench]:
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
    if bench == LONGMEMEVAL:
        cases, skipped = read_longmemeval(next(iter(paths.values())))
    else:
        cases, skipped = read_memoryagentbench(paths)
    out = datasets.data_dir(cache, bench)
    out.mkdir(parents=True, exist_ok=True)
    with (out / "cases.jsonl.tmp").open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(out / "cases.jsonl.tmp", out / "cases.jsonl")
    questions = [q for c in cases for q in c["questions"]]
    fetched = {
        "bench": bench,
        "files": files,
        "tasks": len(questions),
        "cases": len(cases),
        "labelled": sum(1 for q in questions if q["answers"] is not None),
        "abstention": sum(1 for q in questions if q["answers"] is not None and is_abstention(q["id"])),
        "unmatched": sum(q["unmatched"] for q in questions),
        "skipped": skipped,
    }
    (out / "fetched.json").write_text(json.dumps(fetched, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return fetched


def load_cases(cache: Path, bench: str) -> list[dict]:
    path = datasets.data_dir(cache, bench) / "cases.jsonl"
    try:
        with path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        raise BenchError(f"{bench} is not fetched: run `evo-agents kg bench fetch {bench}`") from None


# -- the graph of a case -------------------------------------------------------------------------------------------


def case_part(case: dict) -> str:
    """The part of a case's unscored questions: of its first labelled question, else of its id."""
    for q in case["questions"]:
        if q["answers"] is not None:
            return question_part(q["id"])
    return repo_split(f"{case['bench']}/{case['id']}")


def part_of(case: dict, question: dict) -> str:
    return question_part(question["id"]) if question["answers"] is not None else case_part(case)


def stand_in(case: dict) -> Task:
    """The case as the Task the graph cache names, sizes and evicts graphs by (``graphs.Layout``)."""
    return Task(
        bench=case["bench"],
        id=case["id"],
        key=case["id"],
        repo=f"{case['bench']}/{case['id']}",
        commit=ITEMS_VERSION,
        language="chat",
        question="",
    )


def _when(date: str | None, fallback: int) -> str:
    """A session's date as kg/1 time; a session without one counts days from 2000-01-01, in its order."""
    found = DATE.search(date or "")
    if found:
        y, mo, d, h, mi = (int(x) for x in found.groups())
        with contextlib.suppress(ValueError):
            return datetime(y, mo, d, h, mi, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.fromtimestamp(946684800 + fallback * 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def items(case: dict) -> list[dict]:
    """The kg/1 items of a case (see the module), unfinished: chats are numbered from 1 in session order, messages
    from 1 across the case, so a result's id names its session (``session_of_node``)."""
    out, message = [], 0
    label = {"level": "public", "integrity": "U"}
    for number, session in enumerate(case["sessions"], 1):
        sent = _when(session.get("date"), number)
        chat = f"{SOURCE_ID}:chat:{number}"
        title = f"chat {number}" + (f" of {session['date']}" if session.get("date") else "")
        root = message + 1
        thread = f"{SOURCE_ID}:thread:{number}/{root}"
        out.append(
            {"id": chat, "kind": "chat", "title": title, "rev_time": sent, "props": {"chat": number}, "label": label}
        )
        out.append(
            {
                "id": thread,
                "kind": "thread",
                "title": f"{title} (thread {root})",
                "parent": chat,
                "rev_time": sent,
                "props": {"chat": number, "root": root},
                "label": label,
            }
        )
        for seq, turn in enumerate(session["turns"], 1):
            message += 1
            person = turn["role"] == "user"
            author = "user" if person else "@assistant"
            item = {
                "id": f"{SOURCE_ID}:message:{message}",
                "kind": "message",
                "title": f"#{message} {author} {sent}",
                "parent": thread,
                "rev_time": sent,
                "props": {
                    "chat": number,
                    "thread": root,
                    "seq": seq,
                    "author_kind": "person" if person else "agent",
                    "author": author,
                    "sent_at": sent,
                },
                "label": label,
            }
            if turn["content"].strip():
                item["body"] = {"format": "markdown", "text": turn["content"]}
            out.append(item)
    return out


def connector(ctx):
    """The in-process connector of a case's graph: the items of the file its source names, as kg/1 messages."""
    from evo_agents.kg.protocol import finalize_item, hello

    yield hello("bench-memory", ITEMS_VERSION, list_complete=True, rev_exact=True)
    count = 0
    with open(ctx.source["config"]["file"], encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            item = finalize_item({**json.loads(line), "rev": "-", "rev_exact": True})
            item["rev"] = item["hash"]
            count += 1
            yield item
    yield {"type": "listing", "scope": f"{ctx.source['id']}:", "complete": True, "count": count}
    yield {"type": "closed", "status": "ok"}


def write_harness(lay: graphs.Layout, case: dict) -> Path:
    task = stand_in(case)
    root = lay.harness(task)
    root.mkdir(parents=True, exist_ok=True)
    name = lay.name(task)
    with (root / "items.jsonl").open("w", encoding="utf-8") as f:
        for item in items(case):
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
                "connector": f"python:{__name__}:connector",
                "config": {"file": str(root / "items.jsonl")},
                "label": {"level": "public", "integrity": "U"},
            }
        ],
    }
    (root / "knowledge.yaml").write_text(json.dumps(knowledge, indent=1) + "\n", encoding="utf-8")
    return root


def build_case(
    lay: graphs.Layout, case: dict, *, room: graphs.Room = graphs.DEFAULT_ROOM, keep: frozenset[str] = frozenset()
) -> graphs.Built:
    """The graph of a case: reused when built with this extractor and ITEMS_VERSION, else built once ``make_room``
    made room for it (``graphs.build_graph`` for a memory case). DiskFull goes up to the caller."""
    task = stand_in(case)
    if lay.built(task):
        with contextlib.suppress(OSError):
            os.utime(lay.project_dir(task) / graphs.MARKER)
        return graphs.Built(task, reused=True)
    from evo_agents.kg.build import build_project
    from evo_agents.kg.sync import sync_project

    graphs.make_room(lay, task, room, keep | {lay.name(task)})
    start = time.monotonic()
    try:
        write_harness(lay, case)
        with graphs.bench_home(lay.home):
            project = graphs.project_of(lay, task)
            runs = sync_project(project)
            failed = [r for r in runs if not r.ok]
            if failed:
                why = failed[0].exception or "; ".join(failed[0].issues[:2]) or failed[0].status
                return graphs.Built(task, False, time.monotonic() - start, f"sync: {why}")
            report = build_project(project)
            if not report.ok:
                return graphs.Built(task, False, time.monotonic() - start, f"build: {'; '.join(report.errors[:2])}")
    except BenchError as exc:
        return graphs.Built(task, False, time.monotonic() - start, str(exc))
    except Exception as exc:  # a build that crashes fails its case, not the set's whole run
        return graphs.Built(
            task, False, time.monotonic() - start, f"build crashed: {type(exc).__name__}: {str(exc)[:200]}"
        )
    seconds = round(time.monotonic() - start, 1)
    marker = {"repo": task.repo, "commit": task.commit, "extractor": lay.extractor, "seconds": seconds}
    size = graphs.tree_bytes(lay.project_dir(task))
    (lay.project_dir(task) / graphs.MARKER).write_text(json.dumps({**marker, "bytes": size}) + "\n", encoding="utf-8")
    (lay.harness(task) / "items.jsonl").unlink(missing_ok=True)  # the graph holds them now; the cache keeps cases.jsonl
    with (lay.base / "builds.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({**marker, "bench": task.bench}) + "\n")
    return graphs.Built(task, False, seconds)


# -- scoring -------------------------------------------------------------------------------------------------------

_NODE = re.compile(rf"{SOURCE_ID}:(chat|thread|message):(\d+)")


def session_of_node(node_id: str, message_session: list[int]) -> int | None:
    """The index of the session a result is in: its chat, its thread's chat, or its message's (``items``)."""
    found = _NODE.search(node_id or "")
    if not found:
        return None
    kind, number = found.group(1), int(found.group(2))
    if kind == "message":
        return message_session[number - 1] if 0 < number <= len(message_session) else None
    return number - 1


def message_sessions(case: dict) -> list[int]:
    return [i for i, s in enumerate(case["sessions"]) for _ in s["turns"]]


def ndcg_any(ranked: list[int], answers: set[int], k: int) -> float:
    """``eval_utils.ndcg`` of LongMemEval over sessions: relevance 1 for an answer session."""
    gains = [1.0 if s in answers else 0.0 for s in ranked[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(answers))))
    return dcg / ideal if ideal else 0.0


def session_metrics(ranked: list[int], answers: list[int], unmatched: int = 0) -> dict:
    """The metrics of one question (see the module); an answer session no graph holds (``unmatched``) is never found."""
    wanted = set(answers)
    metrics = {}
    for k in KS:
        top = set(ranked[:k])
        metrics[f"recall_all@{k}"] = float(bool(wanted) and not unmatched and wanted <= top)
        metrics[f"recall_any@{k}"] = float(bool(wanted & top))
        metrics[f"ndcg_any@{k}"] = round(ndcg_any(ranked, wanted, k), 4)
    return metrics


def ranked_sessions(nodes: list[dict], message_session: list[int]) -> list[int]:
    out: list[int] = []
    for node in nodes:
        s = session_of_node(str(node.get("id") or ""), message_session)
        if s is not None and s not in out:
            out.append(s)
    return out


def ask(session, text: str, limit: int = LIMIT) -> tuple[list[dict], int, float, str | None]:
    """kg_search asked ``text``: its result nodes, tokens, milliseconds and error."""
    start = time.perf_counter()
    result = session.call("kg_search", {"query": text, "limit": limit})
    ms = (time.perf_counter() - start) * 1000
    body = "\n".join(b.get("text", "") for b in result.get("content") or [] if isinstance(b, dict))
    data = result.get("structuredContent") or {}
    nodes = [] if result.get("isError") else [n for n in data.get("results") or [] if isinstance(n, dict)]
    error = body.removeprefix("error: ")[:300] if result.get("isError") else None
    return nodes, estimate_tokens(body), ms, error


@dataclass
class Options:
    parts: tuple[str, ...]
    limit: int = LIMIT
    sample: int | None = None
    seed: int = 0
    max_hours: float | None = None
    room: graphs.Room = graphs.DEFAULT_ROOM
    jobs: int = 1
    progress: object = None
    scored: dict = field(default_factory=dict)


def _record(case: dict, question: dict, part: str, metrics: dict, tokens=0, ms=0.0, error=None, skipped=None) -> dict:
    return {
        "bench": case["bench"],
        "id": question["id"],
        "part": part,
        "repo": case["id"],
        "language": question["type"],
        "dataset": case["dataset"],
        "method": "kg_search",
        "metrics": metrics,
        "tokens": tokens,
        "ms": round(ms, 1),
        "error": error,
        "skipped": skipped,
    }


def scored(case: dict, question: dict) -> bool:
    return question["answers"] is not None and not is_abstention(question["id"])


def ask_case(cache: Path, case: dict, options: Options) -> dict:
    """Build a case's graph and ask each of its labelled questions in ``options.parts``: its records and build."""
    lay = graphs.layout(cache)
    task = stand_in(case)
    built = build_case(lay, case, room=options.room, keep=frozenset({lay.name(task)}))
    asked = [q for q in case["questions"] if scored(case, q) and part_of(case, q) in options.parts]
    out = {
        "case": case["id"],
        "built": not built.reused and not built.error,
        "reused": built.reused,
        "failed": bool(built.error),
        "seconds": built.seconds,
        "error": built.error,
        "records": [],
    }
    if built.error:
        out["records"] = [
            _record(case, q, part_of(case, q), {}, skipped=f"no graph: {built.error[:200]}") for q in asked
        ]
        return out
    if not asked:
        return out
    from evo_agents.kg.bench.run import session_of

    graphs.warm(lay.project_dir(task))
    where = message_sessions(case)
    with graphs.bench_home(lay.home):
        session = session_of(graphs.project_of(lay, task), "graph")
        for q in asked:
            nodes, tokens, ms, error = ask(session, q["question"], options.limit)
            metrics = session_metrics(ranked_sessions(nodes, where), q["answers"], q["unmatched"])
            out["records"].append(_record(case, q, part_of(case, q), metrics, tokens, ms, error))
    return out


def ask_case_kept(cache: Path, case: dict, options: Options) -> dict:
    """``ask_case``, its answer kept in the cache so a run that dies goes on from the cases it finished."""
    from evo_agents.kg.bench.run import ranking_digest

    lay = graphs.layout(cache)
    key = json.dumps(
        [
            lay.extractor,
            ranking_digest(),
            ITEMS_VERSION,
            case["bench"],
            case["id"],
            options.limit,
            sorted(options.parts),
        ]
    )
    shard = lay.base / "shards" / f"mem-{hashlib.sha256(key.encode()).hexdigest()[:20]}.json"
    with contextlib.suppress(OSError, ValueError):
        # answered before: its graph is reused, not built by this run
        return {**json.loads(shard.read_text(encoding="utf-8")), "built": False, "reused": True, "seconds": 0.0}
    out = ask_case(cache, case, options)
    if not out["failed"]:
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_text(json.dumps(out) + "\n", encoding="utf-8")
    return out


def wanted(case: dict, parts) -> bool:
    """Whether a case is built: one of its questions is in a part asked for and is scored, or it has no labels, so
    that nothing of it is scored and its graph is built whatever its part."""
    labelled = [q for q in case["questions"] if q["answers"] is not None]
    if not labelled:
        return True
    return any(scored(case, q) and part_of(case, q) in parts for q in labelled)


def select(cache: Path, bench: str, options: Options) -> tuple[list[dict], dict | None]:
    """The cases of ``bench`` to build, sampled (by the seed) when asked or when building them would pass max_hours."""
    cases = [c for c in load_cases(cache, bench) if wanted(c, options.parts)]
    size, why = options.sample, f"--sample {options.sample}" if options.sample is not None else None
    if size is None and options.max_hours is not None:
        lay = graphs.layout(cache)
        mean = graphs.build_seconds(lay, bench)
        if mean is not None:
            hours = sum(1 for c in cases if not lay.built(stand_in(c))) * mean / 3600
            if hours > options.max_hours:
                size = max(1, math.floor(len(cases) * options.max_hours / hours))
                why = f"building every graph would take about {hours:.1f} h, over {options.max_hours:g} h"
    if size is None or size >= len(cases):
        return cases, None
    order = sorted(cases, key=lambda c: hashlib.sha256(f"{options.seed}:{bench}:{c['id']}".encode()).hexdigest())
    keep = {c["id"] for c in order[:size]}
    chosen = [c for c in cases if c["id"] in keep]
    return chosen, {
        "size": len(chosen),
        "of": len(cases),
        "seed": options.seed,
        "why": why,
        "ids": [c["id"] for c in chosen],
    }


def _asked_all(cache: Path, cases: list[dict], options: Options) -> list[dict]:
    say = options.progress or (lambda line: None)
    if options.jobs <= 1 or len(cases) <= 1:
        out = []
        for n, case in enumerate(cases, 1):
            out.append(ask_case_kept(cache, case, options))
            say(_progress(n, len(cases), case, out[-1]))
        return out
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    quiet = Options(**{**options.__dict__, "progress": None})
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=options.jobs, mp_context=context) as pool:
        futures = [pool.submit(ask_case_kept, cache, case, quiet) for case in cases]
        try:
            out = []
            for n, (case, future) in enumerate(zip(cases, futures, strict=True), 1):
                out.append(future.result())
                say(_progress(n, len(cases), case, out[-1]))
            return out
        except BaseException:  # a full disk (graphs.DiskFull) or a crash: no case not started yet starts
            pool.shutdown(wait=True, cancel_futures=True)
            raise


def _progress(n: int, total: int, case: dict, done: dict) -> str:
    state = "reused" if done["reused"] else f"failed: {done['error']}" if done["failed"] else f"{done['seconds']}s"
    return f"{case['bench']} {n}/{total} {case['id']}: {len(done['records'])} asked, {state}"


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 4) if values else None


def numbers(records: list[dict]) -> dict:
    """n and the mean of every metric of ``records`` (asked, not skipped)."""
    mine = [r for r in records if r["skipped"] is None]
    return {"n": len(mine), **{m: _mean(r["metrics"].get(m) for r in mine) for m in METRICS}}


def part_numbers(records: list[dict], by: str = "language") -> dict:
    """A part of a set: n, the primary metric, tokens, p95, every metric, and the same per question type and ability
    (``languages`` names the question types, as the code sets' languages, so ``show`` and ``compare`` read it)."""
    mine = [r for r in records if r["skipped"] is None]
    types: dict[str, list[dict]] = {}
    abilities: dict[str, list[dict]] = {}
    for r in mine:
        types.setdefault(r["language"], []).append(r)
        abilities.setdefault(ABILITIES.get(r["language"], r.get("dataset") or "other"), []).append(r)
    return {
        "n": len(mine),
        "primary": _mean(r["metrics"].get(PRIMARY) for r in mine),
        "mean_tokens": _mean(r["tokens"] for r in mine),
        "p95_ms": round(p95([r["ms"] for r in mine]), 1),
        "precision": None,
        "skipped": len(records) - len(mine),
        "metrics": numbers(mine),
        "languages": {
            t: {"n": len(v), "primary": _mean(r["metrics"].get(PRIMARY) for r in v)} for t, v in sorted(types.items())
        },
        "abilities": {a: numbers(v) for a, v in sorted(abilities.items())},
    }


def dataset_rows(cases: list[dict], results: dict[str, dict], parts) -> dict:
    """Per dataset of a set: its cases, questions, sessions and messages, the graphs built, and how retrieval was
    scored: the metrics per part, or NO_LABELS."""
    out: dict[str, dict] = {}
    for case in cases:
        row = out.setdefault(
            case["dataset"],
            {
                "ability": case["ability"],
                "cases": 0,
                "questions": 0,
                "sessions": 0,
                "messages": 0,
                "graphs": 0,
                "failed": 0,
                "labelled": 0,
            },
        )
        done = results.get(case["id"]) or {}
        row["cases"] += 1
        row["questions"] += len(case["questions"])
        row["sessions"] += len(case["sessions"])
        row["messages"] += sum(len(s["turns"]) for s in case["sessions"])
        row["graphs"] += int(bool(done) and not done.get("failed"))
        row["failed"] += int(bool(done.get("failed")))
        row["labelled"] += sum(1 for q in case["questions"] if q["answers"] is not None)
    for name, row in out.items():
        records = [r for c in cases if c["dataset"] == name for r in (results.get(c["id"]) or {}).get("records", [])]
        if not row["labelled"]:
            row["retrieval"] = NO_LABELS
        else:
            row["retrieval"] = {p: numbers([r for r in records if r["part"] == p]) for p in parts}
    return out


def run_memory(cache: Path, bench: str, options: Options) -> tuple[dict, list[dict]]:
    """Build the graphs of the selected cases of ``bench`` and ask their scored questions: the set's part of the
    summary and its task records."""
    cases, picked = select(cache, bench, options)
    results = _asked_all(cache, cases, options)
    by_id = {r["case"]: r for r in results}
    records = [r for done in results for r in done["records"]]
    every = load_cases(cache, bench)
    part = {
        "kind": "memory",
        "revision": revision(bench),
        "fetched": [
            {k: f[k] for k in ("dataset", "file", "sha256")}
            for f in (datasets.fetched_info(cache, bench) or {}).get("files", [])
        ],
        "primary": PRIMARY,
        "method": "kg_search",
        "limit": options.limit,
        "granularity": "session",
        "abstention": "left out, as LongMemEval's print_retrieval_metrics.py",
        "tasks": sum(1 for c in every for q in c["questions"] if scored(c, q) and part_of(c, q) in options.parts),
        "deduped": 0,
        "asked": len(records),
        "sample": picked,
        "build": {
            "cases": len(cases),
            "built": sum(1 for r in results if r["built"]),
            "reused": sum(1 for r in results if r["reused"]),
            "failed": sum(1 for r in results if r["failed"]),
            "seconds": round(sum(r["seconds"] for r in results), 1),
        },
        "datasets": dataset_rows(cases, by_id, options.parts),
        "parts": {p: part_numbers([r for r in records if r["part"] == p]) for p in options.parts},
    }
    return part, records


def build_memory(cache: Path, bench: str, options: Options) -> tuple[list[dict], list[graphs.Built]]:
    """Build the graphs of the selected cases of ``bench``, scoring nothing."""
    lay = graphs.layout(cache)
    cases, _ = select(cache, bench, options)
    say = options.progress or (lambda line: None)
    done = []
    for n, case in enumerate(cases, 1):
        built = build_case(lay, case, room=options.room, keep=frozenset({lay.name(stand_in(case))}))
        done.append(built)
        state = "reused" if built.reused else f"failed: {built.error}" if built.error else f"{built.seconds}s"
        say(f"{bench} {n}/{len(cases)} {case['id']} {state}")
    return cases, done
