"""`kg bench` on the memory sets: a small LongMemEval and MemoryAgentBench made here, nothing downloaded
(evo_agents.kg.bench.memory, docs/kg-bench.md)."""

import json
from collections import namedtuple
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.kg.bench import compare, datasets, graphs, memory
from evo_agents.kg.bench.datasets import Source

Usage = namedtuple("Usage", "total used free")
ROOMY = Usage(4000 * graphs.GB, 0, 2000 * graphs.GB)

LME = Source("t/lme", "1" * 40, "lme.json")
MAB_AR = Source("t/mab", "2" * 40, "ar.jsonl")
MAB_CR = Source("t/mab", "2" * 40, "cr.jsonl")

TOPICS = [
    ("I adopted a greyhound called Biscuit from the shelter last week.", "Greyhounds are gentle dogs."),
    ("My sourdough starter finally doubled overnight in the warm kitchen.", "A warm kitchen helps a starter rise."),
    ("We booked a kayak trip along the fjords of Norway for August.", "The fjords are lovely in August."),
    ("I switched my commute to an electric bicycle with a cargo box.", "Cargo bikes carry a lot."),
    ("My grandmother taught me to knit a cable sweater in teal wool.", "Teal wool suits a cable pattern."),
    ("The quarterly tax return for my bakery is due on the fifteenth.", "Mark the fifteenth in your calendar."),
]


def _id(prefix: str, part: str, start: int = 0) -> str:
    """The first question id from ``prefix<n>`` that falls in ``part``."""
    n = start
    while memory.question_part(f"{prefix}{n}") != part:
        n += 1
    return f"{prefix}{n}"


def _session(n: int) -> list[dict]:
    user, assistant = TOPICS[n]
    return [{"role": "user", "content": user, "has_answer": True}, {"role": "assistant", "content": assistant}]


def _lme_row(qid: str, question: str, answer: int, kind: str = "single-session-user") -> dict:
    return {
        "question_id": qid,
        "question_type": kind,
        "question": question,
        "question_date": "2023/06/01 (Thu) 10:00",
        "answer": "x",
        "answer_session_ids": [f"s{answer}"],
        "haystack_session_ids": [f"s{n}" for n in range(len(TOPICS))],
        "haystack_dates": [f"2023/05/{10 + n:02d} (Wed) 09:{n:02d}" for n in range(len(TOPICS))],
        "haystack_sessions": [_session(n) for n in range(len(TOPICS))],
    }


def _write(path: Path, rows, as_list: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(rows) if as_list else "".join(json.dumps(r) + "\n" for r in rows)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def sets(tmp_path, monkeypatch, kg_env):
    """Both memory sets in a mirror (JSON in place of their parquet), and the cache: (cache, mirror, ids)."""
    monkeypatch.setattr(graphs, "disk_usage", lambda path: ROOMY)
    monkeypatch.setattr(memory, "SOURCES", {memory.LONGMEMEVAL: (LME,), memory.MEMORYAGENTBENCH: (MAB_AR, MAB_CR)})
    monkeypatch.setattr(memory, "MAB_ABILITY", {MAB_AR.file: memory.MAB_ACCURATE, MAB_CR.file: memory.MAB_FORGETTING})
    ids = {
        "holdout": _id("q", "holdout"),
        "tune": _id("q", "tune"),
        "abs": _id("q", "holdout", 1) + "_abs",
        "mab": _id("m", "holdout"),
    }
    mirror = tmp_path / "mirror"
    _write(
        mirror / LME.dataset / LME.file,
        [
            _lme_row(ids["holdout"], "What is the name of the greyhound I adopted from the shelter?", 0),
            _lme_row(ids["tune"], "When is the quarterly tax return for my bakery due?", 5, "knowledge-update"),
            _lme_row(ids["abs"], "What colour is my kayak?", 2),
        ],
        as_list=True,
    )
    chats = []
    for n in range(len(TOPICS)):
        chats += [
            f"Chat Time: 2023/05/{10 + n:02d} (Wed) 09:00",
            [{k: t[k] for k in ("role", "content")} for t in _session(n)],
        ]
    labelled = {
        "context": repr(chats),
        "questions": [
            "Current Date: 2023/06/01\n\n Now Answer the Question: Which wool colour did I knit a sweater in?"
        ],
        "answers": [["teal"]],
        "metadata": {
            "source": "longmemeval_s*",
            "question_ids": [ids["mab"]],
            "question_types": ["single-session-user"],
            "haystack_sessions": [[_session(4)]],
        },
    }
    novel = {
        "context": "\n\n".join(f"Chapter {n}. " + "The lighthouse keeper counted ships. " * 30 for n in range(8)),
        "questions": ["What did the keeper count?", "Who kept the lighthouse?"],
        "answers": [["ships"], ["the keeper"]],
        "metadata": {"source": "eventqa_full", "previous_events": [[], []]},
    }
    facts = {
        "context": "Here is a list of facts:\n0. The capital of Zembla is Ora.\n1. The capital of Zembla is Vel.\n",
        "questions": ["What is the capital of Zembla?"],
        "answers": [["Vel"]],
        "metadata": {"source": "factconsolidation_sh_6k"},
    }
    _write(mirror / MAB_AR.dataset / MAB_AR.file, [labelled, novel])
    _write(mirror / MAB_CR.dataset / MAB_CR.file, [facts])
    return tmp_path / "cache", mirror, ids


def test_a_session_is_a_chat_of_one_thread_whose_turns_are_its_messages():
    case = {
        "bench": memory.LONGMEMEVAL,
        "id": "q1",
        "dataset": "longmemeval_s",
        "ability": None,
        "sessions": [
            {"date": "2023/05/20 (Sat) 02:21", "turns": [{"role": "user", "content": "hi"}]},
            {"date": None, "turns": [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]},
        ],
        "questions": [],
    }
    items = memory.items(case)
    assert [i["id"] for i in items] == [
        "mem:chat:1",
        "mem:thread:1/1",
        "mem:message:1",
        "mem:chat:2",
        "mem:thread:2/2",
        "mem:message:2",
        "mem:message:3",
    ]
    first, reply = items[2], items[6]
    assert first["parent"] == "mem:thread:1/1" and first["rev_time"] == "2023-05-20T02:21:00Z"
    assert first["title"] == "#1 user 2023-05-20T02:21:00Z" and first["body"] == {"format": "markdown", "text": "hi"}
    assert first["props"] == {
        "chat": 1,
        "thread": 1,
        "seq": 1,
        "author_kind": "person",
        "author": "user",
        "sent_at": "2023-05-20T02:21:00Z",
    }
    assert reply["props"]["author_kind"] == "agent" and reply["props"]["author"] == "@assistant"
    assert reply["props"]["seq"] == 2 and reply["parent"] == "mem:thread:2/2"
    where = memory.message_sessions(case)
    assert [memory.session_of_node(n, where) for n in ("mem:message:3", "mem:thread:2/2", "mem:chat:1")] == [1, 1, 0]
    assert memory.session_of_node("mem:message:9", where) is None and memory.session_of_node("doc:x:y", where) is None
    nodes = [{"id": "mem:message:2"}, {"id": "mem:chat:2"}, {"id": "mem:message:1"}, {"id": "other"}]
    assert memory.ranked_sessions(nodes, where) == [1, 0]


def test_scores_are_those_of_the_sets_retrieval_script():
    assert memory.session_metrics([3, 1, 0], [1])["recall_all@5"] == 1.0
    two = memory.session_metrics([3, 1, 4, 5, 6, 0], [1, 0])
    assert two["recall_all@5"] == 0.0 and two["recall_any@5"] == 1.0 and two["recall_all@10"] == 1.0
    assert two["ndcg_any@5"] == round((1 / 1.584962500721156) / (1 + 1 / 1.584962500721156), 4)
    assert memory.session_metrics([1, 0], [1, 0], unmatched=1)["recall_all@5"] == 0.0  # one never found
    assert memory.session_metrics([], [2])["ndcg_any@10"] == 0.0
    assert memory.is_abstention("gpt4_2655b836_abs") and not memory.is_abstention("gpt4_2655b836")
    assert memory.question_part("0a995998") == memory.question_part("0A995998")


def test_a_context_is_its_sessions_or_its_chunks():
    sessions = memory.context_sessions(repr(["Chat Time: 2023/05/20 (Sat) 02:21", [{"role": "user", "content": "x"}]]))
    assert sessions == [{"date": "2023/05/20 (Sat) 02:21", "turns": [{"role": "user", "content": "x"}]}]
    assert memory.context_sessions("Document 1: text") is None and memory.context_sessions("[1, 2, 3]") is None
    text = "\n\n".join("w" * 900 for _ in range(5))
    parts = memory.chunks(text, 2048)
    assert all(len(p) <= 2048 for p in parts) and "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert memory.chunks("a" * 5000, 2048) == ["a" * 2048, "a" * 2048, "a" * 904]


def test_fetch_reads_both_sets_and_names_the_unlabelled_ones(sets):
    cache, mirror, ids = sets
    assert (
        main(
            [
                "kg",
                "bench",
                "fetch",
                memory.LONGMEMEVAL,
                memory.MEMORYAGENTBENCH,
                "--cache",
                str(cache),
                "--from",
                str(mirror),
            ]
        )
        == 0
    )
    lme = memory.load_cases(cache, memory.LONGMEMEVAL)
    assert [c["id"] for c in lme] == [ids["holdout"], ids["tune"], ids["abs"]]
    assert len(lme[0]["sessions"]) == 6 and lme[0]["questions"][0]["answers"] == [0]
    mab = {c["dataset"]: c for c in memory.load_cases(cache, memory.MEMORYAGENTBENCH)}
    assert set(mab) == {"longmemeval_s*", "eventqa_full", "factconsolidation_sh_6k"}
    assert (
        mab["longmemeval_s*"]["questions"][0]["answers"] == [4]
        and mab["longmemeval_s*"]["questions"][0]["id"] == ids["mab"]
    )
    assert all(q["answers"] is None for q in mab["eventqa_full"]["questions"])
    assert len(mab["eventqa_full"]["sessions"]) == 1 and len(mab["eventqa_full"]["sessions"][0]["turns"]) > 1
    assert mab["factconsolidation_sh_6k"]["ability"] == memory.MAB_FORGETTING
    fetched = datasets.fetched_info(cache, memory.MEMORYAGENTBENCH)
    assert fetched["labelled"] == 1 and fetched["tasks"] == 4 and fetched["files"][0]["revision"] == "2" * 40


def test_run_scores_the_labelled_questions_and_builds_every_graph(sets, kg_env, tmp_path, capsys):
    cache, mirror, ids = sets
    argv = ["--cache", str(cache)]
    assert (
        main(["kg", "bench", "fetch", memory.LONGMEMEVAL, memory.MEMORYAGENTBENCH, *argv, "--from", str(mirror)]) == 0
    )
    out = tmp_path / "results" / "memory.json"
    assert main(["kg", "bench", "run", memory.LONGMEMEVAL, memory.MEMORYAGENTBENCH, *argv, "--out", str(out)]) == 0
    capsys.readouterr()
    summary = compare.load_summary(out)
    lme, mab = summary["benches"][memory.LONGMEMEVAL], summary["benches"][memory.MEMORYAGENTBENCH]
    assert lme["primary"] == "recall_all@5" and lme["limit"] == memory.LIMIT and "test" not in lme["parts"]
    assert lme["revision"] == {"t/lme": "1" * 40}
    assert lme["parts"]["holdout"]["n"] == 1 and lme["parts"]["tune"]["n"] == 1  # the abstention one left out
    assert lme["build"]["built"] == 2 and lme["build"]["failed"] == 0
    held = next(r for r in summary["tasks"] if r["id"] == ids["holdout"])
    assert held["metrics"]["recall_all@5"] == 1.0 and held["error"] is None and held["skipped"] is None
    assert lme["parts"]["holdout"]["abilities"]["information_extraction"]["n"] == 1
    assert lme["parts"]["tune"]["languages"] == {
        "knowledge-update": {"n": 1, "primary": lme["parts"]["tune"]["primary"]}
    }
    assert not any(r["id"] == ids["abs"] for r in summary["tasks"])

    rows = mab["datasets"]
    assert rows["eventqa_full"]["retrieval"] == memory.NO_LABELS
    assert rows["factconsolidation_sh_6k"]["retrieval"] == memory.NO_LABELS
    assert rows["longmemeval_s*"]["retrieval"]["holdout"]["n"] == 1
    assert mab["parts"]["holdout"]["n"] == 1 and mab["parts"]["holdout"]["primary"] == 1.0
    assert {d: r["graphs"] for d, r in rows.items()} == {d: 1 for d in rows}  # unlabelled: built whatever its part

    required = ["--require", f"{memory.LONGMEMEVAL},{memory.MEMORYAGENTBENCH}", "--split", "holdout"]
    assert main(["kg", "bench", "show", str(out), *required]) == 0
    # a second run reuses every graph and every answer
    again = tmp_path / "results" / "again.json"
    assert main(["kg", "bench", "run", memory.LONGMEMEVAL, *argv, "--out", str(again)]) == 0
    second = compare.load_summary(again)["benches"][memory.LONGMEMEVAL]
    assert second["build"]["built"] == 0 and second["parts"] == lme["parts"]


def test_a_memory_set_takes_the_graph_search_and_a_limit_kg_search_has(sets, capsys):
    cache, mirror, _ = sets
    assert main(["kg", "bench", "fetch", memory.LONGMEMEVAL, "--cache", str(cache), "--from", str(mirror)]) == 0
    argv = ["kg", "bench", "run", memory.LONGMEMEVAL, "--cache", str(cache)]
    assert main([*argv, "--search", "bm25", "--out", "x.json"]) == 1
    assert main([*argv, "--limit", "40", "--out", "x.json"]) == 1
    err = capsys.readouterr().err
    assert "--search graph only" in err and "--limit 1 to 25" in err
    assert main(["kg", "bench", "fetch", "nosuchset", "--cache", str(cache)]) == 1
