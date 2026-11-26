// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import claudeCode from "@/test/fixtures/trace/claude-code.json";
import opencode from "@/test/fixtures/trace/opencode.json";
import { renderVi } from "@/test/render";

import { AgentTrace, type TraceContext } from "./agent-trace";
import type { RunEvent } from "./queries";

const context: TraceContext = {
  runId: 12,
  owner: "octo",
  viewer: "octo",
  describe: (move) => `${move.from} > ${move.to}`,
  openDecisions: new Set(),
  working: false,
  active: false,
};

function show(events: unknown[], extra: Partial<TraceContext> = {}) {
  return renderVi(<AgentTrace events={events as RunEvent[]} status="ended" context={{ ...context, ...extra }} />);
}

let seq = 0;
const ev = (kind: string, body: Record<string, unknown>, second = 0) => ({
  seq: ++seq,
  at: `2026-10-07T03:00:${String(second).padStart(2, "0")}.000Z`,
  kind,
  body,
  truncated: false,
});

describe("AgentTrace", () => {
  it("is a polite log of the run's items, its calls grouped by what they did, a failed one open under its row", async () => {
    show(claudeCode.events);
    const log = screen.getByRole("log", { name: "Trace của run #12" });
    expect(log).toHaveAttribute("aria-live", "polite");
    const groups = within(log).getAllByTestId("trace-group");
    expect(groups[0]).toHaveAttribute("data-intent", "explore");

    // The failed call shows open under its group's row, which counts it; the others fold into their group.
    expect(within(groups[0]).getByTestId("trace-group-title")).toHaveTextContent("1 lỗi");
    const failed = within(groups[0]).getByTestId("trace-tool");
    expect(failed).toHaveAttribute("open");
    expect(failed).toHaveAttribute("data-status", "failed");
    expect(within(failed).getByTestId("trace-exit")).toHaveTextContent("mã thoát 1");
    expect(within(failed).getByTestId("trace-tool-arg")).toHaveTextContent("cat missing-file.txt");
    expect(within(failed).getByTestId("trace-tool-output")).toHaveTextContent("No such file or directory");
    expect(within(failed).getByTestId("trace-duration")).toHaveTextContent("1,4 giây");

    // A group opens on its row, a line per call; a line opens on what the call printed.
    const run = groups.find((group) => group.getAttribute("data-intent") === "run" && group.textContent?.includes("selftest.txt"))!;
    expect(within(run).queryAllByTestId("trace-tool")).toHaveLength(0);
    const user = userEvent.setup();
    await user.click(within(run).getByTestId("trace-group-toggle"));
    const line = within(run).getAllByTestId("trace-tool").find((tool) => tool.textContent?.includes("selftest.txt"))!;
    expect(line).not.toHaveAttribute("open");
    await user.click(within(line).getByTestId("trace-tool-arg"));
    expect(line).toHaveAttribute("open");
    expect(within(line).getByTestId("trace-tool-output")).toHaveTextContent("(Bash completed with no output)");

    // The runtime's events no adapter reads are left to the Raw log behind one line; the agent's words are shown, moves
    // are said.
    expect(within(log).queryByTestId("trace-raw")).toBeNull();
    expect(within(log).getByTestId("trace-raw-left")).toHaveTextContent("Raw log");
    expect(within(log).getAllByTestId("trace-message").map((node) => node.textContent)).toEqual(["STARTED", expect.stringContaining("selftest.txt")]);
    expect(log).toHaveTextContent("running > verifying");
    expect(screen.queryByTestId("trace-typing")).toBeNull();
  });

  it("folds the agent's thinking under how long it took", async () => {
    show(opencode.events);
    const thoughts = screen.getAllByTestId("trace-thought");
    expect(thoughts).toHaveLength(4);
    expect(thoughts[3]).toHaveTextContent("Suy nghĩ trong 3,2 giây");
    expect(thoughts[3]).not.toHaveAttribute("open");
    await userEvent.setup().click(within(thoughts[3]).getByText(/Suy nghĩ trong/));
    expect(thoughts[3]).toHaveAttribute("open");
    expect(thoughts[3]).toHaveTextContent("Done. Reply with one short sentence.");
  });

  it("cuts a long output at 20 lines with Show the full output", async () => {
    seq = 0;
    const text = Array.from({ length: 45 }, (_, index) => `line ${index + 1}`).join("\n");
    show([
      ev("tool_call", { toolCallId: "c1", title: "Bash", kind: "execute", rawInput: { command: "seq 45" } }, 1),
      ev("tool_call_update", { toolCallId: "c1", status: "failed", rawOutput: { exitCode: 3 }, content: [{ type: "content", content: { type: "text", text } }] }, 2),
    ]);
    const output = screen.getByTestId("trace-tool-output");
    expect(output).toHaveTextContent("line 20");
    expect(output).not.toHaveTextContent("line 21");
    const more = within(output).getByTestId("trace-output-more");
    expect(more).toHaveTextContent("Hiện toàn bộ output (45 dòng)");
    expect(more).toHaveAttribute("aria-expanded", "false");
    await userEvent.setup().click(more);
    expect(output).toHaveTextContent("line 45");
    expect(more).toHaveAttribute("aria-expanded", "true");
  });

  it("wraps the note the hub's hook left in a cut output, and keeps the log's lines as written", () => {
    seq = 0;
    const note = "[evo-hub: this output has 26,344 characters and only its start and end are shown here; all of it is in /tmp/out.txt (kept 7 days).]";
    const text = ["tests/test_a.py::test_one PASSED    [ 50%]", "", note, "", "tests/test_b.py::test_two PASSED    [100%]"].join("\n");
    show([
      ev("tool_call", { toolCallId: "c1", title: "Bash", kind: "execute", rawInput: { command: "uv run pytest -v" } }, 1),
      ev("tool_call_update", { toolCallId: "c1", status: "failed", rawOutput: { exitCode: 1 }, content: [{ type: "content", content: { type: "text", text } }] }, 2),
    ]);
    const output = screen.getByTestId("trace-tool-output");
    const pre = output.querySelector("pre");
    expect(pre).toHaveClass("whitespace-pre");
    expect(pre?.textContent).toBe(text);
    const wrapped = within(output).getByTestId("trace-hub-note");
    expect(wrapped).toHaveClass("block", "whitespace-pre-wrap");
    expect(wrapped.textContent).toBe(`${note}\n`);
  });

  it("links Asked you to the decision's card while it is open on the page, to the Inbox otherwise", () => {
    seq = 0;
    const events = [ev("system", { text: "decision #7 asked (scope): Publish now?", decision: { id: 7, step: "13", category: "scope" } })];
    const first = show(events, { openDecisions: new Set([7]) });
    expect(screen.getByText("Hỏi bạn")).toBeInTheDocument();
    expect(screen.getByTestId("trace-ask-question")).toHaveTextContent("Publish now?");
    expect(screen.getByTestId("trace-ask-link")).toHaveAttribute("href", "#run-decision-7");
    expect(screen.getByTestId("trace-ask-link")).toHaveTextContent("Trả lời");
    first.unmount();

    show(events, { viewer: "mona" });
    expect(screen.getByText("Hỏi octo")).toBeInTheDocument();
    expect(screen.getByTestId("trace-ask-link")).toHaveAttribute("href", "/inbox?decision=7");
  });

  it("says who wrote a message, and ends a running run with the typing dots", () => {
    seq = 0;
    show(
      [
        ev("user_message", { text: "also cover the revoked token", from: "octo", message_id: 1 }),
        ev("user_message", { text: "Decision #7: A", from: "mona", decision_id: 7 }),
        ev("plan", { entries: [{ content: "Write the test", status: "completed" }, { content: "Fix it", status: "in_progress" }] }),
      ],
      { working: true, active: true },
    );
    const items = screen.getAllByTestId("trace-item");
    expect(items[0]).toHaveTextContent("Bạn");
    expect(items[1]).toHaveTextContent("mona đã trả lời quyết định #7");
    expect(within(items[2]).getByTestId("trace-plan")).toHaveTextContent("Write the test");
    expect(items[2]).toHaveTextContent("Xong 1 trên 2");
    expect(screen.getByTestId("trace-typing")).toHaveTextContent("Agent đang làm việc.");
  });

  it("shows a step's report as a card, its verify folded when it passed", async () => {
    seq = 0;
    const text = [
      "step: 1",
      "result: pass",
      "commits: d4664cd",
      "branch: feat/worker-settings-web",
      "files: evo_agents/hub/runs.py, tests/hub/test_runs.py (new)",
      "outside_scope: none",
      "verify_command: ruff check .",
      "verify_output: |",
      "  All checks passed!",
      "decisions: |",
      "  Decision: The answer key is `settings`.",
      "  Decision: A body with no field set is 422.",
      "notes: Step 2 must read the new key.",
    ].join("\n");
    show([ev("agent_message_chunk", { content: { type: "text", text } })], {
      webOfBranch: () => ({ url: "https://github.com/octo/evo", forge: "github" }),
    });
    const card = await screen.findByTestId("trace-report");
    expect(card).toHaveAttribute("data-result", "pass");
    expect(card).toHaveAccessibleName("Báo cáo bước 1");
    expect(within(card).getByTestId("trace-report-result")).toHaveTextContent("Đạt");
    expect(within(card).getByRole("link", { name: /d4664cd/ })).toHaveAttribute("href", "https://github.com/octo/evo/commit/d4664cd");
    expect(within(card).getByRole("link", { name: /feat\/worker-settings-web/ })).toHaveAttribute(
      "href",
      "https://github.com/octo/evo/tree/feat/worker-settings-web",
    );
    const decisions = within(card).getByTestId("trace-report-decisions");
    expect(within(decisions).getAllByRole("listitem")).toHaveLength(2);
    expect(within(decisions).getByText("settings").tagName).toBe("CODE");
    expect(within(card).getByTestId("trace-report-notes")).toHaveTextContent("Step 2 must read the new key.");
    expect(within(card).getByTestId("trace-report-files")).toHaveTextContent("2 file, 1 file mới");
    const verify = within(card).getByTestId("trace-report-verify");
    expect(verify).not.toHaveAttribute("open");
    await userEvent.setup().click(within(verify).getByText("Kiểm tra"));
    expect(within(verify).getByTestId("trace-report-output")).toHaveTextContent("All checks passed!");
    expect(screen.queryByTestId("trace-message")).toBeNull();
  });

  it("shows one delegation card for the subagents handed work at once, their own events left to their profiles", async () => {
    const meta = (parent: string) => ({ _meta: { parentToolCallId: parent } });
    const text = (words: string) => ({ content: { type: "text", text: words } });
    const out = (words: string) => [{ type: "content", content: { type: "text", text: words } }];
    const events = [
      ev("agent_message_chunk", text("Chia việc cho hai sub-agent."), 0),
      ev("tool_call", { toolCallId: "toolu_s2", title: "Agent", kind: "other", rawInput: { description: "Step 2", subagent_type: "plan-step", prompt: "plan: org-chart\nstep: 2\nRun the step." } }, 1),
      ev("tool_call", { toolCallId: "toolu_s3", title: "Agent", kind: "other", rawInput: { description: "Step 3", subagent_type: "plan-step", run_in_background: true, prompt: "plan: org-chart\nstep: 3" } }, 2),
      // The subagents' own words and calls: in their profiles, not in the main agent's trace.
      ev("agent_message_chunk", { ...text("SUBAGENT WORDS"), ...meta("toolu_s2") }, 3),
      ev("tool_call", { toolCallId: "c9", title: "Bash", kind: "execute", rawInput: { command: "uv run pytest -q" }, ...meta("toolu_s2") }, 4),
      ev("tool_call_update", { toolCallId: "c9", status: "completed", content: out("12 passed"), ...meta("toolu_s2") }, 5),
      ev("tool_call_update", { toolCallId: "toolu_s2", status: "completed", content: out("step: 2\nresult: pass\n\nagentId: a1b2 <usage>total_tokens: 9</usage>") }, 6),
    ];
    const opened: string[] = [];
    show(events, { active: true, working: true, openAgent: (id) => opened.push(id) });
    const log = screen.getByRole("log");
    expect(log).not.toHaveTextContent("SUBAGENT WORDS");
    expect(within(log).queryByText(/uv run pytest/)).toBeNull();

    const cards = within(log).getAllByTestId("trace-delegation");
    expect(cards).toHaveLength(1);
    expect(log).toHaveTextContent("Giao việc cho 2 sub-agent");
    const agents = within(cards[0]).getAllByTestId("trace-delegation-agent");
    expect(agents.map((agent) => agent.getAttribute("data-tool-call-id"))).toEqual(["toolu_s2", "toolu_s3"]);
    expect(within(agents[0]).getByTestId("trace-delegation-name")).toHaveTextContent("Step 2");
    expect(agents[0]).toHaveTextContent("plan-step");
    expect(within(agents[0]).getByTestId("trace-delegation-brief")).toHaveTextContent("plan: org-chart step: 2 Run the step.");
    // Its report without the runtime's trailer; the one still at work says so.
    expect(agents[0]).toHaveAttribute("data-status", "completed");
    expect(within(agents[0]).getByTestId("trace-delegation-outcome")).toHaveTextContent("step: 2 result: pass");
    expect(within(agents[0]).getByTestId("trace-delegation-outcome")).not.toHaveTextContent("agentId");
    expect(agents[1]).toHaveAttribute("data-status", "running");
    expect(agents[1]).toHaveTextContent("chạy nền");
    expect(within(agents[1]).getByTestId("trace-delegation-outcome")).toHaveTextContent("Đang làm việc");

    await userEvent.setup().click(within(agents[1]).getByTestId("trace-delegation-open"));
    expect(opened).toEqual(["toolu_s3"]);
  });

  it("keeps a run whose events name no subagent as it was, each delegation's subagent's events in its profile's trace", () => {
    const events = [
      ev("tool_call", { toolCallId: "toolu_old", title: "Agent", kind: "other", rawInput: { description: "Old step", prompt: "Do it." } }, 1),
      ev("tool_call", { toolCallId: "c1", title: "Bash", kind: "execute", rawInput: { command: "ls" } }, 2),
      ev("tool_call", { toolCallId: "c2", title: "Bash", kind: "execute", rawInput: { command: "ls -la" }, _meta: { parentToolCallId: "toolu_x" } }, 3),
    ];
    const { unmount } = show(events);
    // No id: the call stays in the main agent's trace, next to the delegation card of the call that started a subagent.
    expect(screen.getAllByTestId("trace-delegation")).toHaveLength(1);
    expect(screen.getAllByTestId("trace-group")).toHaveLength(1);
    expect(screen.getByRole("log")).toHaveTextContent("ls");
    unmount();
    renderVi(<AgentTrace events={events as RunEvent[]} status="ended" context={context} agent="toolu_x" />);
    expect(screen.queryByTestId("trace-delegation")).toBeNull();
    expect(screen.getAllByTestId("trace-group")).toHaveLength(1);
  });

  it("says why it is empty", () => {
    show([], { active: true });
    expect(screen.getByTestId("trace-empty")).toHaveTextContent("Đang chờ sự kiện đầu tiên của run…");
  });
});
