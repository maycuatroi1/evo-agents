"""The deterministic facets of sessions and runs (``evo_agents.hub.facets``, ``evo_agents.hub.facet_readers``).

Step 3 of plan history-driven-curator, criterion a3 (its deterministic part): a facet made without any model from a
session's transcripts (Claude Code and Codex, subagents' included), a run's events or a session's digest, holding the
tools and the failures by cause, the commands and those run again, the files read and edited, the calls of the
knowledge graph and whether the files edited after them were in their results, the context at the start and at its
peak, the compactions, the output per tool, the tokens with cache reads and writes, the cost, the time of each hook,
the skills loaded and the corrections; the same content makes the same facet."""

import json

from evo_agents.hub import facets
from evo_agents.hub.facet_readers import RunFacet, run_facet, session_facet, tool_name
from tests.hub.test_digest import (
    FAILED_TEST,
    MODEL,
    USAGE,
    answered,
    at,
    codex_rollout,
    item,
    result,
    said,
    session_lines,
    tokens,
    tool_use,
    used,
)

KG_RESULT = {
    "build": 1,
    "results": [
        {"id": "symbol:app:app/search.py::search", "kind": "Symbol", "props": {"path": "app/search.py", "line": 5}},
        {"id": "app:file:app/rank.py", "kind": "File", "props": {"path": "app/rank.py", "repo": "app"}},
        {"id": "app:file:docs/guide.md#guide", "kind": "Section"},
    ],
}
EMPTY_RESULT = {"build": 1, "results": [], "summary": "0 results"}
KG = "mcp__plugin_evo-kg_evo-kg__kg_search"


def numbered(lines: list) -> list[tuple[int, object]]:
    return list(enumerate(lines, start=1))


def hooked(name: str, command: str, ms: int, kind: str = "hook_success", **extra) -> dict:
    found = {"type": kind, "hookName": name, "hookEvent": name.split(":")[0], "command": command, "durationMs": ms}
    return {"type": "attachment", "attachment": {**found, **extra}, "timestamp": at(0)}


def claude_session() -> list[dict]:
    """session_lines and a knowledge graph search whose result names the file edited after it, an empty search and
    the grep after it, two hooks, a skill, a compaction, the cost, an API error and a correction."""
    hook = "command -v uvx >/dev/null 2>&1 && uvx --offline --from evo-ak==0.21.0 evo-agents hub hook stop || true"
    return [
        hooked("SessionStart:startup", "python3 /Users/alice/.claude/skills/update/check.py --notice", 950),
        *session_lines(),
        said("không, đừng chạy lại toàn bộ test", 15),
        used("msg_6", 16, 90_000, tool_use("toolu_6", KG, query="search")),
        result("toolu_6", 17, json.dumps(KG_RESULT)),
        answered(
            "msg_7", 18, tool_use("toolu_7", "Edit", file_path="/work/app/app/rank.py", old_string="a", new_string="b")
        ),
        result("toolu_7", 19),
        answered("msg_8", 20, tool_use("toolu_8", KG, query="nothing")),
        result("toolu_8", 21, json.dumps(EMPTY_RESULT)),
        answered("msg_9", 22, tool_use("toolu_9", "Grep", pattern="claim")),
        result("toolu_9", 25, "queue.py:1"),
        answered("msg_10", 26, tool_use("toolu_10", "Skill", skill="stop-slop")),
        result("toolu_10", 27, "Launching skill: stop-slop"),
        {"type": "attachment", "attachment": {"type": "invoked_skills", "skills": [{"name": "stop-slop"}]}},
        hooked("PreToolUse:Bash", "evo-agents kg hook pre-search", 5367, "hook_cancelled", timedOut=True),
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "hookInfos": [{"command": hook, "durationMs": 414}, {"command": "callback"}],
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "c-1",
            "compactMetadata": {"trigger": "auto", "preTokens": 167_289},
            "timestamp": at(28),
        },
        {"type": "system", "subtype": "api_error", "error": {"formatted": "429 rate_limit_error: slow down"}},
        {"type": "cost-state", "totalCostUSD": 1.25},
        {"type": "cost-state", "totalCostUSD": 2.5},
    ]


def subagent() -> list[dict]:
    """A subagent's transcript: its calls count, its context and its prompt do not."""
    return [
        said("Find where claim is defined.", 30) | {"isSidechain": True},
        used("msg_s1", 31, 700_000, tool_use("toolu_s1", "Bash", command="grep -rn claim /work/app"))
        | {"isSidechain": True},
        result("toolu_s1", 32, "permission denied: /work/app/secret", error=True) | {"isSidechain": True},
        {"type": "cost-state", "totalCostUSD": 99.0},
    ]


def test_a_claude_code_session_facet_holds_its_tools_failures_commands_files_and_tokens():
    body = session_facet([("main", numbered(claude_session())), ("subagents/agent-a.jsonl", numbered(subagent()))])
    assert body["version"] == facets.FACET_VERSION and body["model"] == MODEL
    assert body["files"] == [{"file": "main", "lines": 35}, {"file": "subagents/agent-a.jsonl", "lines": 4}]
    tools = {item["gen_ai.tool.name"]: item for item in body["tools"]}
    assert (tools["Bash"]["calls"], tools["Bash"]["errors"]) == (4, 3), "the subagent's call counts"
    assert tools["Bash"]["output_chars"] == len(FAILED_TEST) + len("12 passed") + len("rejected: non-fast-forward")
    assert tools["Bash"]["timed"] == 4 and tools["Bash"]["ms"] == 4000 and tools["Grep"]["ms"] == 3000
    assert tools["Grep"]["buckets"][facets.bucket(3000)] == 1
    assert body["bash"][0] == {"program": "python -m pytest", "calls": 2, "errors": 1}
    causes = {item["text"]: (item["cause"], item["at"]) for item in body["errors"]}
    assert causes[FAILED_TEST] == ("work", "main:6")
    assert causes["permission denied: /work/app/secret"] == ("os_permission", "subagents/agent-a.jsonl:3")
    assert body["causes"] == [{"cause": "work", "n": 2}, {"cause": "os_permission", "n": 1}]
    assert body["commands"][0] == "cd /work/app && python -m pytest -q" and body["command_at"][0] == "main:5"
    assert body["repeated_commands"] == [{"command": "cd /work/app && python -m pytest -q", "n": 2, "at": "main:5"}]
    assert body["files_read"] == ["/work/app/queue.py"]
    assert body["files_edited"] == ["/work/app/queue.py", "/work/app/app/rank.py"]
    assert body["user_turns"] == ["Run the tests and fix what fails.", "không, đừng chạy lại toàn bộ test"]
    assert body["corrections"] == [1], "the subagent's prompt is not the person's"


def test_a_session_facet_holds_the_graph_context_compactions_cost_hooks_skills_and_api_errors():
    body = session_facet([("main", numbered(claude_session())), ("subagents/agent-a.jsonl", numbered(subagent()))])
    assert body["kg"] == {
        "calls": 2,
        "empty": 1,
        "tools": [{"tool": "kg_search", "calls": 2, "empty": 1}],
        "searches": 2,
        "fallbacks": 1,
        "edited": 1,
        "in_results": 1,
        "in_top": 1,
        "lookups": 2,
        "tokens": facets.kg_tokens(json.dumps(KG_RESULT)) + facets.kg_tokens(json.dumps(EMPTY_RESULT)),
        "lookup_tokens": [facets.kg_tokens(json.dumps(KG_RESULT)), facets.kg_tokens(json.dumps(EMPTY_RESULT))],
        "answer": {"lookups": 1, "tokens": facets.kg_tokens(json.dumps(KG_RESULT))},
        "chains": 2,
        "grep_after": 1,
        "queries": 1,
        "clicked": 1,
        "click_ranks": {"2": 1},
    }, "the grep after the empty search falls back; the subagent's grep comes after the graph's calls"
    first, empty = body["kg_labels"]
    assert (first["query"], first["at"], first["grep_after"]) == ("search", "main:20", False)
    assert [r["rank"] for r in first["results"]] == [1, 2, 3] and first["results"][1]["path"] == "app/rank.py"
    assert first["clicks"] == [{"id": "app:file:app/rank.py", "path": "app/rank.py", "rank": 2, "how": "edit"}]
    assert (empty["query"], empty["results"], empty["grep_after"]) == ("nothing", [], True), "the Grep came right after"
    assert body["context"] == {"first": 4310, "peak": 90_000}, "the subagent's context is its own"
    assert body["compactions"] == 2 and body["compaction"] == {"auto": 2, "largest_tokens": 167_289}, (
        "and session_lines' own"
    )
    assert body["usage"] == {
        "input_tokens": USAGE["input_tokens"] * 9 + 2,
        "output_tokens": USAGE["output_tokens"] * 9 + 10,
        "cache_creation_input_tokens": USAGE["cache_creation_input_tokens"] * 9 + 18,
        "cache_read_input_tokens": USAGE["cache_read_input_tokens"] * 9 + 89_990 + 699_990,
    }
    assert body["cost_usd"] == 2.5, "the largest cost-state of the main file"
    hooks = {(item["hook"], item["command"]): item for item in body["hooks"]}
    start = hooks[("SessionStart:startup", "python3 ~/.claude/skills/update/check.py --notice")]
    assert (start["runs"], start["ms"], start["failed"], start["timed_out"]) == (1, 950, 0, 0)
    stop = hooks[
        ("Stop", "command -v uvx >/dev/null 2>&1 && uvx --offline --from evo-ak==N evo-agents hub hook stop || true")
    ]
    assert stop["ms"] == 414, "a version number does not split a hook"
    cancelled = hooks[("PreToolUse:Bash", "evo-agents kg hook pre-search")]
    assert (cancelled["timed_out"], cancelled["failed"], cancelled["max_ms"]) == (1, 0, 5367)
    assert body["skills"] == [{"skill": "stop-slop", "calls": 1, "returned": 1, "chars": 0}]
    assert body["api_errors"] == [{"text": "429 rate_limit_error: slow down", "n": 1, "cause": "rate_limit"}]


def test_the_same_content_makes_the_same_facet_and_its_summary_adds_up():
    lines = [("main", numbered(claude_session())), ("subagents/agent-a.jsonl", numbered(subagent()))]
    first = session_facet(lines)
    again = session_facet([("main", numbered(claude_session())), ("subagents/agent-a.jsonl", numbered(subagent()))])
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    numbers = facets.summary(first)
    assert (numbers["tool_calls"], numbers["tool_errors"], numbers["environment_errors"]) == (11, 3, 1)
    assert (numbers["corrections"], numbers["kg_calls"], numbers["kg_empty"], numbers["compactions"]) == (1, 2, 1, 2)
    assert (numbers["context_first"], numbers["context_peak"], numbers["cost_usd"]) == (4310, 90_000, 2.5)
    assert numbers["cache_read_tokens"] == first["usage"]["cache_read_input_tokens"]
    assert numbers["hook_ms"] == 950 + 414 + 5367


def test_a_line_that_is_not_json_or_not_an_object_is_skipped():
    body = session_facet([("main", [(1, None), (2, "text"), (3, [1, 2]), *numbered(session_lines())[3:]])])
    assert body["messages"] > 0 and body["files"] == [{"file": "main", "lines": 16}]
    assert session_facet([])["messages"] == 0


def test_a_codex_session_adds_up_the_tokens_of_its_threads():
    rollout = codex_rollout()
    thread = [
        rollout[0],
        tokens(10, 50_000, {"input_tokens": 5000, "cached_input_tokens": 4000, "output_tokens": 30}),
        item(11, type="CommandExecution", id="c9", command=["rg", "claim"], exit_code=0, aggregated_output="x"),
    ]
    body = session_facet([("main", numbered(rollout)), ("subagents/t-2.jsonl", numbered(thread))], "codex")
    assert body["runtime"] == "codex" and body["model"] == "gpt-6-astra"
    assert body["usage"] == {
        "input_tokens": 11_000 + 1000,
        "output_tokens": 530,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 34_000,
    }
    assert body["context"] == {"first": 120_000, "peak": 150_000}, "a subagent's context is its own"
    assert body["compactions"] == 1 and body["files_edited"] == ["/work/proj/queue.py"]
    tools = {entry["gen_ai.tool.name"]: entry for entry in body["tools"]}
    assert (tools["shell"]["calls"], tools["shell"]["errors"]) == (3, 1)
    assert body["kg"]["calls"] == 1 and body["kg"]["empty"] == 0, "a text that does not say 0 results is not empty"
    assert body["user_turns"] == ["Run the tests and fix what fails."] and body["cost_usd"] is None


def event(seq: int, what: str, second: int = 0, **body) -> dict:
    return {"seq": seq, "at": at(second), "kind": what, "body": body}


def run_events() -> list[dict]:
    return [
        event(1, "system", text="checkout"),
        event(2, "tool_call", 1, toolCallId="t1", title="Bash", kind="execute", rawInput={"command": "make test"}),
        event(
            3,
            "tool_call_update",
            4,
            toolCallId="t1",
            status="failed",
            content=[{"type": "content", "content": {"type": "text", "text": "curl: connection refused"}}],
        ),
        event(4, "agent_message_chunk", 5, content={"type": "text", "text": "Trying again."}),
        event(5, "user_message", 6, text="don't retry, read the log first", **{"from": "owner"}),
        event(6, "tool_call", 7, toolCallId="t2", title="Edit", kind="edit", rawInput={"file_path": "/w/app.py"}),
        event(7, "tool_call_update", 8, toolCallId="t2", status="completed"),
        event(8, "tool_call_update", 9, toolCallId="t2", status="completed"),  # once
        event(
            9,
            "output",
            10,
            raw={
                "type": "system",
                "subtype": "compact_boundary",
                "uuid": "u1",
                "compact_metadata": {"trigger": "auto", "pre_tokens": 99_000},
            },
        ),
    ]


def test_a_run_facet_reads_its_events_and_takes_the_usage_and_cost_of_the_run():
    usage = {
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 4000,
        "total_cost_usd": 0.42,
        "peak_context_tokens": 77_000,
        "first_request_cache_read_input_tokens": 12_000,
        "first_request_cache_creation_input_tokens": 3000,
    }
    body = run_facet("claude-code", run_events(), usage, 0.42)
    tools = {entry["gen_ai.tool.name"]: entry for entry in body["tools"]}
    assert (tools["Bash"]["calls"], tools["Bash"]["errors"], tools["Bash"]["ms"]) == (1, 1, 3000)
    assert tools["Edit"]["calls"] == 1 and body["files_edited"] == ["/w/app.py"]
    assert body["errors"] == [
        {"gen_ai.tool.name": "Bash", "text": "curl: connection refused", "n": 1, "cause": "network", "at": 3}
    ]
    assert body["commands"] == ["make test"] and body["command_at"] == [2]
    assert body["user_turns"] == ["don't retry, read the log first"] and body["corrections"] == [0]
    assert body["context"] == {"first": 15_000, "peak": 77_000}
    assert body["compaction"] == {"auto": 1, "largest_tokens": 99_000}
    assert body["usage"]["cache_read_input_tokens"] == 4000 and body["cost_usd"] == 0.42
    assert body["files"] == [{"file": "events", "lines": 9}]
    builder = RunFacet("claude-code")
    for found in run_events():
        builder.add(found)
    assert builder.body(usage, 0.42) == body


def test_a_codex_run_names_its_calls_by_kind_and_reads_its_cached_tokens():
    assert tool_name("claude-code", "Bash", "execute") == "Bash"
    assert tool_name("codex", "make test", "execute") == "execute"
    assert tool_name("codex", "evo-kg/kg_search", "other") == "evo-kg/kg_search"
    events = [
        event(1, "tool_call", 1, toolCallId="c1", title="rg claim", kind="execute", rawInput={"command": "rg claim"}),
        event(2, "tool_call_update", 2, toolCallId="c1", status="completed"),
        event(3, "tool_call", 3, toolCallId="c2", title="evo-kg/kg_search", kind="other", rawInput={"query": "x"}),
        event(4, "tool_call_update", 4, toolCallId="c2", status="completed", rawOutput={"results": []}),
    ]
    body = run_facet("codex", events, {"input_tokens": 900, "cached_input_tokens": 600, "output_tokens": 5})
    assert body["usage"] == {
        "input_tokens": 300,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 600,
    }
    assert body["bash"] == [{"program": "rg", "calls": 1, "errors": 0}]
    assert body["kg"]["calls"] == 1 and body["kg"]["empty"] == 1 and body["cost_usd"] is None


def test_a_digest_facet_keeps_the_digests_lists_and_says_null_for_what_it_does_not_know():
    digest = {
        "messages": 12,
        "model": MODEL,
        "usage": {
            "input_tokens": 5,
            "output_tokens": 6,
            "cache_creation_input_tokens": 7,
            "cache_read_input_tokens": 8,
        },
        "tools": [{"gen_ai.tool.name": "Bash", "calls": 4, "errors": 2}],
        "tool_result_chars": [{"gen_ai.tool.name": "Bash", "chars": 900}],
        "bash": [{"program": "sleep", "calls": 2, "errors": 2}],
        "errors": [{"gen_ai.tool.name": "Bash", "text": "Blocked: sleep 30 is not allowed", "n": 2}],
        "commands": ["sleep 30"],
        "repeated_commands": [{"command": "sleep 30", "n": 2}],
        "user_turns": ["fix it", "no, not like that"],
        "files_edited": ["/w/a.py"],
        "peak_context_tokens": 50_000,
        "compactions": 1,
    }
    body = facets.digest_facet(digest)
    assert body["tools"] == [{"gen_ai.tool.name": "Bash", "calls": 4, "errors": 2, "output_chars": 900}]
    assert body["errors"][0]["cause"] == "harness_blocked" and body["causes"] == [{"cause": "harness_blocked", "n": 2}]
    assert body["corrections"] == [1] and body["context"] == {"first": None, "peak": 50_000}
    assert (body["kg"], body["hooks"], body["skills"], body["cost_usd"], body["files_read"]) == (None,) * 5
    numbers = facets.summary(body)
    assert (numbers["tool_errors"], numbers["environment_errors"], numbers["cache_write_tokens"]) == (2, 2, 7)
    assert facets.summary(facets.digest_facet(None))["messages"] == 0


def test_the_paths_of_a_kg_result_and_the_name_of_a_kg_tool():
    assert facets.kg_paths(json.dumps(KG_RESULT)) == (3, ["app/search.py", "app/rank.py", "docs/guide.md"])
    assert facets.kg_paths("not json") == (0, [])
    assert facets.kg_op(KG) == "kg_search" and facets.kg_op("mcp__evo-hub__kg_impact") == "kg_impact"
    assert facets.kg_op("evo-kg/kg_context") == "kg_context" and facets.kg_op("Bash") is None
    assert facets.hook_command("uvx --from evo-ak==0.22.1 x  /home/bob/y") == "uvx --from evo-ak==N x ~/y"


SEARCH_TEXT = """2 result(s) for 'claim a job' in project demo (build 7)
label of every node unless its line names another: internal
[app:app/queue.py::Queue.claim] Symbol 'Queue.claim' (parsed) lines 12-30
    > the [worker] claims a job: see [notes](x)
  mentions [app:docs/queue.md#claiming] Section 'Claiming' (parsed) lines 80-120 of 10-200
[plan:jobs/step:2] PlanStep 'Claim' (parsed) status=done
[more: call kg_more with handle 'h1' for the rest]"""


def test_a_kg_result_in_text_names_its_nodes_by_their_readable_ids_and_is_empty_only_when_it_says_0():
    nodes, empty = facets.kg_nodes(SEARCH_TEXT)
    assert (
        nodes
        == [
            ("app:app/queue.py::Queue.claim", "app/queue.py"),
            ("app:docs/queue.md#claiming", "docs/queue.md"),
            ("plan:jobs/step:2", None),
        ]
        and not empty
    ), "a snippet, a [more: ...] note and a markdown link name no node"
    assert facets.kg_paths(SEARCH_TEXT) == (3, ["app/queue.py", "docs/queue.md"])
    with open("tests/kg/fixtures/hooks/post_tool_kg_context_text.json", encoding="utf-8") as handle:
        context = json.load(handle)["tool_response"][0]["text"]
    found, paths = facets.kg_paths(context)
    assert found == 33 and paths[:3] == ["app/search.py", "bindings/core.yaml", "docs/guide.md"]
    assert "commit:de2d05e9a1087df1db5bf5718ae77dbf99eca5ba" not in paths, "a commit names no file"
    for text in (
        "0 result(s) for 'x' in project demo (build 7)",
        "impact of 0 seed(s) within 2 hop(s) (build 7):",
        "context for x: 0 nodes, 0 edges (build 7, connected through the seeds)",
        "nothing visible matches; try kg_search",
        json.dumps({"build": 1, "results": []}),
        json.dumps({"content": [{"type": "text", "text": "0 result(s) for 'x' in project demo (build 7)"}]}),
    ):
        assert facets.kg_nodes(text) == ([], True), text
    assert facets.kg_nodes("context for x: 10 nodes, 3 edges (build 7)") == ([], False), "not 0: not empty"
    wrapped = json.dumps({"content": [{"type": "text", "text": SEARCH_TEXT}]})
    assert facets.kg_paths(wrapped) == (3, ["app/queue.py", "docs/queue.md"]), "a content item's text is read"
    assert facets.kg_tokens("abcd") == 2 and facets.kg_tokens("é") == 1 and facets.kg_tokens("") == 0


def test_the_files_a_shell_command_prints():
    command = "cd /w && sed -n '1,80p' app/queue.py | head -20; cat README.md 2>/dev/null && grep -n x y.py"
    assert facets.shell_reads(command) == ["app/queue.py", "README.md"]
    assert facets.shell_reads("bash -lc \"head -n 5 docs/queue.md && sed 's/a/b/' z.py\"") == ["docs/queue.md"]
    assert facets.shell_reads("python -m pytest -q tests/x.py") == []


def kg_session(*after: dict, text: str = SEARCH_TEXT) -> list[dict]:
    """A kg_search whose result is ``text``, then the calls ``after``, each answered."""
    lines = [said("Where is a job claimed?", 0), answered("m0", 1, tool_use("k0", KG, query="claim a job"))]
    lines.append(result("k0", 2, text))
    for n, call in enumerate(after, start=1):
        lines += [answered(f"m{n}", 2 + n, call), result(call["id"], 2 + n)]
    return lines


def test_a_kg_search_is_labelled_with_the_files_of_its_results_read_within_its_window():
    body = session_facet(
        [
            (
                "main",
                numbered(
                    kg_session(
                        tool_use("b1", "Bash", command="sed -n 1,40p /w/app/app/queue.py"),
                        *[tool_use(f"x{n}", "Bash", command="ls") for n in range(facets.KG_WINDOW - 2)],
                        tool_use("r1", "Read", file_path="/w/app/docs/queue.md"),
                        tool_use("e1", "Edit", file_path="/w/app/docs/queue.md", old_string="a", new_string="b"),
                    )
                ),
            )
        ]
    )
    kg = body["kg"]
    assert (kg["lookups"], kg["tokens"], kg["lookup_tokens"]) == (1, facets.kg_tokens(SEARCH_TEXT), [kg["tokens"]])
    assert kg["answer"] == {"lookups": 1, "tokens": kg["tokens"]}, "the first file a result named that was read"
    assert (kg["chains"], kg["grep_after"], kg["queries"], kg["clicked"], kg["click_ranks"]) == (1, 0, 1, 1, {"1": 1})
    (label,) = body["kg_labels"]
    assert label["query"] == "claim a job" and [r["rank"] for r in label["results"]] == [1, 2, 3]
    assert label["clicks"] == [
        {"id": "app:app/queue.py::Queue.claim", "path": "app/queue.py", "rank": 1, "how": "bash"},
        {"id": "app:docs/queue.md#claiming", "path": "docs/queue.md", "rank": 2, "how": "read"},
    ], "the read is the window's last call; the edit after it is outside"
    assert (label["grep_after"], label["after_grep"]) == (False, [])


def test_a_grep_right_after_a_chain_of_lookups_marks_its_searches_and_the_files_read_after_it():
    lines = kg_session(
        tool_use("k1", "mcp__evo-hub__kg_context", query="claim"),
        tool_use("g1", "Grep", pattern="claim"),
        tool_use("r1", "Read", file_path="/w/app/app/jobs.py"),
        tool_use("r2", "Read", file_path="/w/app/app/queue.py"),
    )
    body = session_facet([("main", numbered(lines))])
    kg = body["kg"]
    assert (kg["lookups"], kg["chains"], kg["grep_after"], kg["searches"]) == (2, 1, 1, 1), "one chain of two"
    (label,) = body["kg_labels"]
    assert label["grep_after"] and label["after_grep"] == ["/w/app/app/jobs.py"], "a file no result named"
    assert [c["rank"] for c in label["clicks"]] == [1]
    later = session_facet(
        [
            ("main", numbered(kg_session())),
            ("subagents/agent-a.jsonl", numbered([answered("s1", 9, tool_use("g2", "Grep", pattern="claim"))])),
        ]
    )
    assert later["kg"]["grep_after"] == 0 and not later["kg_labels"][0]["grep_after"], "another thread's grep"
