"""The runtime and the harness of the night's figures, as the hub models them (``evo_agents.hub.runtime_metrics``), the
sources of context a facet reads from a Claude Code transcript (``evo_agents.hub.facet_readers``), and the lenses
runtime and harness with their metrics in the ledger (``evo_agents.hub.review``, ``evo_agents.hub.ledger``).

Step 9 of plan history-driven-curator, criterion a8 (its model): tokens by source (the always-loaded files, the
instructions of MCP servers, the skill list, hooks, the output of tools, MCP tools apart), the shares of the prompt
cache read and written, the cost of each session, run and merged pull request, the peak context, the compactions, the
median and the 95th percentile of the time of each hook and tool, the size of the always-loaded files by repo; a lens
on each with metrics in the ledger; proposal #22, the tokens and the peak context of the sessions. No database."""

import json
from datetime import date, timedelta

from evo_agents.hub import facets, ledger, review, runtime_metrics
from evo_agents.hub.facet_readers import run_facet, session_facet
from evo_agents.hub.runtime_metrics import Item, bucket_quantile, quantile
from tests.hub.test_digest import answered, at, codex_rollout, result, tool_use
from tests.hub.test_facets import claude_session, hooked, numbered, subagent

MCP_TOOL = "mcp__plugin_evo-hub_evo-hub__plan_show"
AGENTS = "a" * 4000
USER_RULES = "u" * 400
KG_BLOCK = "## evo-kg\n" + "k" * 990
HUB_BLOCK = "## evo-hub\n" + "h" * 489
SKILLS = "s" * 8000
HOOK_TEXTS = ("update: 1.0 -> 1.1", "evo-kg: project app", "evo-hub: 0 memories")


def attached(kind: str, **body) -> dict:
    return {"type": "attachment", "attachment": {"type": kind, **body}, "timestamp": at(0)}


def instructions(*files: tuple[str, str, str]) -> dict:
    return attached("instructions", files=[{"path": path, "type": kind, "content": text} for path, kind, text in files])


def context_session() -> list[dict]:
    """claude_session with what the harness put into its main thread: two always-loaded files and the user's, the
    instructions of two MCP servers, the skill list, the text of two hooks (one a list), and an MCP tool's output."""
    return [
        instructions(
            ("/Users/alice/github/app/CLAUDE.md", "Project", "@AGENTS.md"),
            ("/Users/alice/github/app/AGENTS.md", "Project", AGENTS),
            ("/Users/alice/.claude/CLAUDE.md", "User", USER_RULES),
        ),
        attached("mcp_instructions_delta", addedBlocks=[KG_BLOCK, HUB_BLOCK], addedNames=["evo-kg", "evo-hub"]),
        attached("skill_listing", content=SKILLS, skillCount=2),
        hooked("SessionStart:startup", "python3 check.py --notice", 120, content=HOOK_TEXTS[0]),
        attached("hook_additional_context", content=list(HOOK_TEXTS[1:]), hookName="SessionStart"),
        *claude_session(),
        instructions(("/Users/alice/github/app/AGENTS.md", "Project", AGENTS)),  # again, after a /clear
        answered("msg_m", 40, tool_use("toolu_m", MCP_TOOL, plan_id="p")),
        result("toolu_m", 41, "p" * 2000),
    ]


def subagent_with_rules() -> list[dict]:
    return [instructions(("/Users/alice/github/app/AGENTS.md", "Project", AGENTS)), *subagent()]


# The sources of context in a facet


def test_a_claude_code_facet_says_what_each_source_put_into_its_main_thread():
    files = [("main", numbered(context_session())), ("subagents/agent-a.jsonl", numbered(subagent_with_rules()))]
    body = session_facet(files, "claude-code")
    assert body["sources"] == {
        "always_loaded": len("@AGENTS.md") + 2 * len(AGENTS) + len(USER_RULES),
        "mcp_instructions": len(KG_BLOCK) + len(HUB_BLOCK),
        "skills": len(SKILLS),
        "hooks": sum(len(text) for text in HOOK_TEXTS),
    }, "the subagent's files are its own context, and a hook without content says nothing"
    assert body["loaded"] == [
        {"path": "~/github/app/AGENTS.md", "type": "Project", "chars": 4000, "loads": 2},
        {"path": "~/.claude/CLAUDE.md", "type": "User", "chars": 400, "loads": 1},
        {"path": "~/github/app/CLAUDE.md", "type": "Project", "chars": 10, "loads": 1},
    ], "the home directory as ~/, the largest first"
    tools = {entry["gen_ai.tool.name"]: entry for entry in body["tools"]}
    assert tools[MCP_TOOL]["output_chars"] == 2000
    assert session_facet(files, "claude-code") == body, "the same content, the same facet"


def test_a_session_without_attachments_counts_from_zero_and_what_the_facet_cannot_see_is_null():
    plain = session_facet([("main", numbered(claude_session()))])
    assert plain["sources"] == dict.fromkeys(facets.CONTEXT_SOURCES, 0) and plain["loaded"] == []
    codex = session_facet([("main", numbered(codex_rollout()))], "codex")
    assert (codex["sources"], codex["loaded"]) == (None, None), "a Codex rollout names no attachment"
    assert (facets.digest_facet({"messages": 3})["sources"], run_facet("claude-code", [])["sources"]) == (None, None)


# The runtime


def facet_body(read=0, write=0, given=0, output=0, *, cost=None, peak=None, first=None, compactions=0, **parts):
    usage = {
        "input_tokens": given,
        "output_tokens": output,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
    }
    compaction = {"auto": compactions, "largest_tokens": 150_000} if compactions else {"largest_tokens": None}
    return {
        "usage": usage,
        "cost_usd": cost,
        "context": {"first": first, "peak": peak},
        "compactions": compactions,
        "compaction": compaction,
        **parts,
    }


def session(name: str, body: dict, messages: int = 10) -> Item:
    return Item(f"session:{name}", name, body, messages=messages, model="claude-opus-5-5")


def run(run_id: int, kind: str, usage: dict, body: dict | None = None, plan_id: str | None = "fleet") -> Item:
    return Item(f"run:{run_id}:1", f"run:{run_id}", body or {}, kind=kind, plan_id=plan_id, usage=usage)


SESSIONS = [
    session("s-heavy", facet_body(9000, 1000, 100, 500, cost=1.5, peak=120_000, first=30_000, compactions=1)),
    session("s-light", facet_body(1000, 500, 50, 100, peak=60_000, first=20_000), messages=4),
    session("s-old", facet_body(peak=None)),  # a digest of an older client: no tokens, no peak
]
RUNS = [
    run(
        7,
        "step",
        {
            "total_cost_usd": 0.4,
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 50,
            "peak_context_tokens": 40_000,
            "first_request_cache_read_input_tokens": 0,
            "first_request_cache_creation_input_tokens": 9000,
        },
        {"compactions": 2, "compaction": {"auto": 1, "manual": 1, "largest_tokens": 90_000}},
    ),
    run(8, "review", {"input_tokens": 900, "cached_input_tokens": 600, "output_tokens": 40}, plan_id=None),  # Codex
]


def test_the_runtime_of_the_sessions_holds_their_tokens_cache_cost_context_and_heaviest():
    found = runtime_metrics.runtime(SESSIONS, RUNS, runtime_metrics.merged_prs([], {}))["sessions"]
    assert found["counted"] == 2, "a session whose source said no tokens is not counted as 0"
    assert found["tokens"] == {"input": 150, "output": 600, "cache_write": 1500, "cache_read": 10_000, "total": 12_250}
    assert (found["cache_read_share"], found["cache_write_share"]) == (0.8584, 0.1288)
    assert (found["priced"], found["cost_usd"], found["cost_per_session"]) == (1, 1.5, 1.5)
    assert found["context"] == {
        "with_peak": 2,
        "peak_max": 120_000,
        "peak_p50": 60_000,
        "peak_p95": 120_000,
        "first_p50": 20_000,
    }
    assert found["compactions"] == {"compacted": 1, "total": 1, "by_trigger": {"auto": 1}, "largest_tokens": 150_000}
    assert [entry["session"] for entry in found["heaviest"]] == ["s-heavy", "s-light"]
    assert found["heaviest"][0] == {
        "session": "s-heavy",
        "messages": 10,
        "model": "claude-opus-5-5",
        "cache_read_tokens": 9000,
        "tokens": 10_600,
        "context_peak": 120_000,
        "cost_usd": 1.5,
        "evidence": "session:s-heavy",
    }


def test_the_runtime_of_the_runs_holds_their_cost_by_kind_and_what_their_first_request_wrote():
    found = runtime_metrics.runtime(SESSIONS, RUNS, runtime_metrics.merged_prs([], {}))["runs"]
    assert found["counted"] == 2 and found["tokens"] == {
        "input": 310,
        "output": 60,
        "cache_write": 50,
        "cache_read": 900,
        "total": 1320,
    }, "Codex's cached input tokens are cache reads"
    assert (found["priced"], found["cost_usd"], found["cost_per_run"]) == (1, 0.4, 0.4)
    assert found["context"]["peak_max"] == 40_000 and found["context"]["first_p50"] == 9000
    assert found["compactions"] == {
        "compacted": 1,
        "total": 2,
        "by_trigger": {"auto": 1, "manual": 1},
        "largest_tokens": 90_000,
    }
    assert found["by_kind"] == [
        {"kind": "step", "runs": 1, "tokens": 380, "cost_usd": 0.4},
        {"kind": "review", "runs": 1, "tokens": 940, "cost_usd": 0.0},
    ]
    assert found["first_request"] == {
        "runs": 1,
        "cache_read_p50": 0,
        "cache_write_p50": 9000,
        "cache_write_tokens": 9000,
        "cold": 1,
    }
    assert [(entry["run"], entry["cost_usd"], entry["evidence"]) for entry in found["heaviest"]] == [
        ("run:7", 0.4, "run:7:1"),
        ("run:8", None, "run:8:1"),
    ]


def test_merged_pull_requests_carry_the_cost_of_the_runs_of_their_plans():
    merged = [
        {"plan_id": "fleet", "repo": "evo-agents", "pr": 12, "merged_at": "2026-10-07T10:00:00+00:00"},
        {"plan_id": "fleet", "repo": "agent-skills", "pr": 3, "merged_at": "2026-10-06T10:00:00+00:00"},
        {"plan_id": "docs", "repo": "evo-agents", "pr": None, "merged_at": "2026-10-05T10:00:00+00:00"},
    ]
    costs = {"fleet": {"runs": 3, "priced": 2, "cost_usd": 1.2}, "docs": {"runs": 1, "priced": 0, "cost_usd": 0.0}}
    found = runtime_metrics.merged_prs(merged, costs)
    assert {key: found[key] for key in ("prs", "plans", "runs", "priced_runs", "cost_usd", "cost_per_pr")} == {
        "prs": 3,
        "plans": 2,
        "runs": 4,
        "priced_runs": 2,
        "cost_usd": 1.2,
        "cost_per_pr": 0.4,
    }
    assert [entry["plan_id"] for entry in found["entries"]] == ["fleet", "docs"]
    assert [pr["repo"] for pr in found["entries"][0]["prs"]] == ["agent-skills", "evo-agents"], "the first merged first"
    assert runtime_metrics.merged_prs([], {})["cost_per_pr"] is None


def test_the_median_and_95th_percentile_come_from_the_buckets_of_the_facets():
    buckets = [0] * (len(facets.BUCKETS_MS) + 1)
    buckets[2] = 10  # ten calls of at most 1 s
    assert bucket_quantile(buckets, 0.5) == 1000 and bucket_quantile(buckets, 0.5, 700) == 700
    buckets[-1] = 90  # ninety above the last bound
    assert (
        bucket_quantile(buckets, 0.95) == facets.BUCKETS_MS[-1] and bucket_quantile(buckets, 0.95, 400_000) == 400_000
    )
    assert bucket_quantile([0] * 9, 0.5) is None and quantile([], 0.5) is None
    assert quantile([5, 1, 3, 2, 4], 0.5) == 3 and quantile([5, 1, 3, 2, 4], 0.95) == 5


def timed(*pairs: tuple[int, int]) -> list[int]:
    """Buckets with ``n`` calls in the bucket of ``ms``, for each (ms, n)."""
    buckets = [0] * (len(facets.BUCKETS_MS) + 1)
    for ms, n in pairs:
        buckets[facets.bucket(ms)] += n
    return buckets


def test_each_hook_and_tool_has_its_mean_median_and_95th_percentile_over_sessions_and_runs():
    stop = "evo-agents hub hook stop"
    sessions = [
        session(
            "s-1", {"hooks": [{"hook": "Stop", "command": stop, "runs": 4, "timed": 4, "ms": 2000, "max_ms": 900}]}
        ),
        session(
            "s-2", {"hooks": [{"hook": "Stop", "command": stop, "runs": 1, "timed": 1, "ms": 5000, "max_ms": 5000}]}
        ),
    ]
    sessions[0].body["hooks"][0]["buckets"] = timed((200, 2), (900, 2))
    sessions[1].body["hooks"][0]["buckets"] = timed((5000, 1))
    sessions[0].body["tools"] = [{"gen_ai.tool.name": "Bash", "timed": 2, "ms": 600, "buckets": timed((300, 2))}]
    runs = [
        run(
            9,
            "step",
            {},
            {"tools": [{"gen_ai.tool.name": "Bash", "timed": 1, "ms": 40_000, "buckets": timed((40_000, 1))}]},
        )
    ]
    found = runtime_metrics.runtime(sessions, runs, runtime_metrics.merged_prs([], {}))
    assert found["hooks"] == [
        {
            "hook": "Stop",
            "command": stop,
            "runs": 5,
            "timed": 5,
            "mean_ms": 1400,
            "p50_ms": 1000,
            "p95_ms": 5000,
            "max_ms": 5000,
            "failed": 0,
            "timed_out": 0,
            "sessions": 2,
        }
    ]
    assert found["hook_ms"] == 7000
    assert found["tools"] == [
        {"tool": "Bash", "timed": 3, "mean_ms": 13_533, "p50_ms": 300, "p95_ms": 100_000, "sessions": 1, "runs": 1}
    ]


# The harness


def harness_session(name: str, sources: dict | None, loaded: list[dict], tools: list[dict]) -> Item:
    return session(name, {"sources": sources, "loaded": loaded, "tools": tools})


def test_the_harness_counts_each_source_in_tokens_and_the_always_loaded_files_by_repo():
    agents = {"path": "~/github/app/AGENTS.md", "type": "Project", "chars": 4000, "loads": 1}
    sessions = [
        harness_session(
            "s-1",
            {"always_loaded": 4400, "mcp_instructions": 1600, "skills": 8000, "hooks": 400},
            [agents, {"path": "~/.claude/CLAUDE.md", "type": "User", "chars": 400, "loads": 1}],
            [{"gen_ai.tool.name": MCP_TOOL, "output_chars": 4000}, {"gen_ai.tool.name": "Bash", "output_chars": 8000}],
        ),
        harness_session(
            "s-2",
            {"always_loaded": 4800, "mcp_instructions": 0, "skills": 8000, "hooks": 0},
            [{**agents, "chars": 4400}, {"path": "~/github/app-wt/feature/CLAUDE.md", "type": "Project", "chars": 400}],
            [{"gen_ai.tool.name": "Bash", "output_chars": 1600}],
        ),
        harness_session("s-digest", None, [], [{"gen_ai.tool.name": "Bash", "output_chars": 99_999}]),
    ]
    spool = {"digests": 2, "spool_results": 3, "spool_original_chars": 90_000, "spool_reads": 1}
    found = runtime_metrics.harness(sessions, ["app", "agent-skills"], spool)
    assert found["sessions"] == 2 and found["chars_per_token"] == 4, "a session whose sources the hub cannot see is out"
    assert found["sources"] == [
        {"source": "always_loaded", "chars": 9200, "tokens": 2300, "share": 0.2255, "sessions": 2},
        {"source": "mcp_instructions", "chars": 1600, "tokens": 400, "share": 0.0392, "sessions": 1},
        {"source": "skills", "chars": 16_000, "tokens": 4000, "share": 0.3922, "sessions": 2},
        {"source": "hooks", "chars": 400, "tokens": 100, "share": 0.0098, "sessions": 1},
        {"source": "mcp_output", "chars": 4000, "tokens": 1000, "share": 0.098, "sessions": 1},
        {"source": "tool_output", "chars": 9600, "tokens": 2400, "share": 0.2353, "sessions": 2},
    ]
    assert (found["fixed_tokens"], found["fixed_tokens_per_session"]) == (6800, 3400)
    loaded = found["always_loaded"]
    assert (loaded["files"], loaded["chars"], loaded["tokens"]) == (3, 5200, 1300), "each file once, at its largest"
    assert [(entry["repo"], entry["dir"], entry["chars"]) for entry in loaded["repos"]] == [
        ("app", None, 4400),
        (None, "~/.claude", 400),
        (None, "~/github/app-wt/feature", 400),
    ], "a file of a directory that names no repo of the project goes by its directory"
    assert loaded["repos"][0]["files"] == [
        {"path": "~/github/app/AGENTS.md", "type": "Project", "chars": 4400, "sessions": 2, "tokens": 1100}
    ]
    assert found["tool_output"] == [
        {"tool": "Bash", "mcp": False, "chars": 9600, "tokens": 2400, "sessions": 2},
        {"tool": MCP_TOOL, "mcp": True, "chars": 4000, "tokens": 1000, "sessions": 1},
    ]
    assert [(entry["session"], entry["fixed_tokens"], entry["evidence"]) for entry in found["heaviest"]] == [
        ("s-1", 3600, "session:s-1"),
        ("s-2", 3200, "session:s-2"),
    ]
    assert found["spool"] == spool
    empty = runtime_metrics.harness([], [], None)
    assert (empty["sessions"], empty["fixed_tokens"], empty["fixed_tokens_per_session"], empty["spool"]) == (
        0,
        0,
        None,
        None,
    )


# The lenses and the ledger


def night(activity: int, *, cost: float, write: int, p95: int, loaded: int, fixed: int, hook_ms: int = 0) -> dict:
    sessions = {"cost_usd": cost, "tokens": {"cache_write": write}, "context": {"peak_p95": p95}}
    sessions["compactions"] = {"total": 1}
    sessions["heaviest"] = [{"session": "s-heavy", "evidence": "session:s-heavy"}]
    sources = [{"source": "always_loaded", "tokens": loaded}, {"source": "skills", "tokens": fixed - loaded}]
    return {
        "sessions": {"digests": activity},
        "runs": {"done": 0},
        "runtime": {
            "sessions": sessions,
            "runs": {"tokens": {"cache_write": 100}, "compactions": {"total": 2}, "heaviest": []},
            "hook_ms": hook_ms,
        },
        "harness": {
            "sources": [*sources, {"source": "tool_output", "tokens": 500}],
            "always_loaded": {"tokens": loaded},
            "heaviest": [{"session": "s-fixed", "evidence": "session:s-fixed"}],
        },
    }


def test_the_lenses_runtime_and_harness_have_their_figures_and_metrics_in_the_ledger():
    assert {"runtime", "harness"} <= set(review.LENSES) and {"runtime", "harness"} <= set(review.FIGURE_KEYS)
    assert list(review.LENSES)[-1] == "kg_health" and len(review.LENSES) == 14
    assert ledger.LENS_METRICS["runtime"] == (
        "session_cost_usd",
        "cache_write_tokens",
        "context_peak_p95",
        "compactions",
        "hook_ms",
    )
    assert ledger.LENS_METRICS["harness"] == ("always_loaded_tokens", "fixed_context_tokens", "tool_output_tokens")
    before = night(4, cost=2.0, write=900, p95=150_000, loaded=1300, fixed=6800, hook_ms=7000)
    values = {key: ledger.read_metric(before, key) for key in (*runtime_metrics.METRICS,)}
    assert values == {
        "session_cost_usd": 2.0,
        "cache_write_tokens": 1000.0,
        "context_peak_p95": 150_000.0,
        "compactions": 3.0,
        "hook_ms": 7000.0,
        "always_loaded_tokens": 1300.0,
        "fixed_context_tokens": 6800.0,
        "tool_output_tokens": 500.0,
    }
    assert ledger.read_metric({}, "session_cost_usd") == 0.0 and ledger.read_metric({"runtime": None}, "hook_ms") == 0.0
    trigger = ledger.trigger_of(before, "harness", [], night="2026-10-19")
    assert [item["key"] for item in trigger["metrics"]] == list(ledger.LENS_METRICS["harness"])
    assert trigger["metrics"][0]["what"].startswith("tokens of the always-loaded files")
    seen = set()
    for offset in range(len(review.LENSES)):
        seen |= set(review.lenses_for(date(2026, 10, 8) + timedelta(days=offset)))
    assert {"runtime", "harness"} <= seen
    prompt = review.build_review_prompt("demo", date(2026, 10, 19), ["runtime", "harness"], [], before)
    assert "- runtime: how the sessions and runs spend" in prompt and "- harness: what the harness puts" in prompt
    assert '"runtime"' in prompt and '"harness"' in prompt


def test_the_prompt_shows_the_figure_of_a_lens_of_the_night_first_and_short_and_last_on_other_nights():
    figures = {
        "tools": [{"tool": "Bash", "errors": 3}],
        "clusters": [{"cluster": 1}],
        "runtime": {"hooks": [{"hook": f"h{n}"} for n in range(12)], "sessions": {"heaviest": list(range(9))}},
        "harness": {"sources": [{"source": "skills"}]},
    }
    tonight = json.loads(review._figures_text(figures, ["cost", "runtime", "security"]))
    assert list(tonight) == ["runtime", "tools", "clusters", "harness"]
    assert len(tonight["runtime"]["hooks"]) == review.LENS_FIGURE_ITEMS == 5
    assert tonight["runtime"]["sessions"]["heaviest"] == [0, 1, 2, 3, 4]
    other = json.loads(review._figures_text(figures, ["tool_errors"]))
    assert list(other) == ["tools", "clusters", "runtime", "harness"], "the long figures come last on other nights"
    longest = {**figures, "runtime": {"hooks": [{"command": "x" * 900} for _ in range(40)]}}
    assert '"tools"' in review._figures_text(longest, ["tool_errors"]), "a long runtime does not push the tools out"


def test_a_level_compares_as_it_is_a_count_per_session_or_run_and_a_cost_from_half_a_dollar():
    before = night(4, cost=2.0, write=900, p95=150_000, loaded=1300, fixed=6800)
    busier = night(8, cost=2.0, write=900, p95=150_000, loaded=1300, fixed=6800)  # twice the activity
    changes = {
        item["key"]: item["change"]
        for item in ledger.outcome_of(ledger.trigger_of(before, "harness", []), busier)["metrics"]
    }
    assert changes == {"always_loaded_tokens": "same", "fixed_context_tokens": "better", "tool_output_tokens": "better"}
    longer = night(4, cost=2.0, write=900, p95=200_000, loaded=1800, fixed=6800)
    outcome = ledger.outcome_of(ledger.trigger_of(before, "runtime", []), longer)
    assert {item["key"]: item["change"] for item in outcome["metrics"]}["context_peak_p95"] == "worse"
    assert outcome["result"] == "revert"
    assert ledger.worse_evidence(longer, outcome) == ["session:s-heavy"]
    worse_harness = ledger.outcome_of(ledger.trigger_of(before, "harness", []), longer)
    assert ledger.worse_evidence(longer, worse_harness) == ["session:s-fixed"]
    assert ledger.compare("session_cost_usd", 0.1, 10, 0.4, 10) == "same"  # under MIN_WORSE_USD
    assert ledger.compare("session_cost_usd", 1.0, 10, 2.0, 10) == "worse"
