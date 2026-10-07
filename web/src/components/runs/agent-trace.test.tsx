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
  it("is a polite log of the run's items, its failed call open and the others folded", async () => {
    show(claudeCode.events);
    const log = screen.getByRole("log", { name: "Trace của run #12" });
    expect(log).toHaveAttribute("aria-live", "polite");
    const tools = within(log).getAllByTestId("trace-tool");
    expect(tools).toHaveLength(3);
    expect(tools[0]).toHaveAttribute("open");
    expect(tools[0]).toHaveAttribute("data-status", "failed");
    expect(within(tools[0]).getByTestId("trace-exit")).toHaveTextContent("mã thoát 1");
    expect(within(tools[0]).getByTestId("trace-tool-arg")).toHaveTextContent("cat missing-file.txt");
    expect(within(tools[0]).getByTestId("trace-tool-output")).toHaveTextContent("No such file or directory");
    expect(within(tools[0]).getByTestId("trace-duration")).toHaveTextContent("1,4 giây");
    expect(tools[1]).not.toHaveAttribute("open");
    expect(within(tools[1]).queryByTestId("trace-tool-output")).toBeNull();

    // A folded row opens on its summary and shows what the call printed.
    await userEvent.setup().click(within(tools[2]).getByTestId("trace-tool-arg"));
    expect(tools[2]).toHaveAttribute("open");
    expect(within(tools[2]).getByTestId("trace-tool-output")).toHaveTextContent("(Bash completed with no output)");

    // The runtime's events no adapter reads are folded raw, the agent's words are shown, moves are said.
    expect(within(log).getAllByTestId("trace-raw")[0]).toHaveTextContent("system hook_response, system init");
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

  it("says why it is empty", () => {
    show([], { active: true });
    expect(screen.getByTestId("trace-empty")).toHaveTextContent("Đang chờ sự kiện đầu tiên của run…");
  });
});
