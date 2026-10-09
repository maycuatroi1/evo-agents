// @vitest-environment jsdom
import { QueryClient, QueryClientProvider, type UseQueryResult } from "@tanstack/react-query";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OverviewDecision } from "@/components/home/model";
import type { Run, RunEvent } from "@/components/runs/queries";
import type { ApiError } from "@/lib/api/errors";

import messages from "../../../messages/vi.json";

import { TAIL_EVENTS } from "./model";
import { RunTile } from "./run-tile";
import { TileSignals } from "./signals";

/** An EventSource the test drives, in place of the browser's (jsdom has none). */
class FakeSource {
  static made: FakeSource[] = [];
  readyState = 0;
  closed = false;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  private listeners = new Map<string, ((event: MessageEvent<string>) => void)[]>();

  constructor(readonly url: string) {
    FakeSource.made.push(this);
  }
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  send(event: RunEvent) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event) }));
  }
  end(state: string, lastSeq: number) {
    for (const listener of this.listeners.get("end") ?? []) listener(new MessageEvent("end", { data: JSON.stringify({ state, last_seq: lastSeq }) }));
  }
}

const RUN = {
  id: 12,
  kind: "step",
  project: "demo",
  plan_id: "rollout",
  step_key: "2",
  title: "Queue with dispatch and claim",
  dispatched_by: "octo",
  worker_id: 3,
  worker: "laptop",
  state: "waiting",
  last_seq: 500,
  queued_at: "2026-10-09T03:00:00Z",
  started_at: "2026-10-09T03:01:00Z",
  finished_at: null,
} as unknown as Run;

const DECISION = { id: 7, project: "demo", run_id: 12, owner: "octo", yours: true } as OverviewDecision;

function result(data: Run | undefined, extra: Partial<UseQueryResult<Run, ApiError>> = {}) {
  return { data, isError: false, isFetching: false, refetch: vi.fn(), ...extra } as unknown as UseQueryResult<Run, ApiError>;
}

function show(query: UseQueryResult<Run, ApiError>, { decision = DECISION as OverviewDecision | null, onClose = vi.fn() } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const signals = new TileSignals();
  const ui = (current: UseQueryResult<Run, ApiError>, waitingOn: OverviewDecision | null) => (
    <NextIntlClientProvider locale="vi" messages={messages} timeZone="Asia/Ho_Chi_Minh">
      <QueryClientProvider client={client}>
        <RunTile tile={{ project: "demo", id: 12 }} query={current} flight={null} decision={waitingOn} viewer="octo" signals={signals} onClose={onClose} />
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
  const view = render(ui(query, decision));
  return { ...view, signals, onClose, update: (next: UseQueryResult<Run, ApiError>, waitingOn: OverviewDecision | null) => view.rerender(ui(next, waitingOn)) };
}

let seq = 0;
const say = (text: string): RunEvent => ({ seq: ++seq, at: "2026-10-09T03:02:00Z", kind: "agent_message_chunk", body: { text }, truncated: false });
const run = (command: string): RunEvent => ({
  seq: ++seq,
  at: "2026-10-09T03:02:01Z",
  kind: "tool_call",
  body: { toolCallId: `call-${seq}`, title: "Bash", rawInput: { command }, status: "in_progress" },
  truncated: false,
});

beforeEach(() => {
  FakeSource.made = [];
  seq = RUN.last_seq - TAIL_EVENTS;
  vi.stubGlobal("EventSource", FakeSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("RunTile", () => {
  it("names the run, links its number to its page, and offers to close it, with no control of the run", async () => {
    const { onClose } = show(result(RUN));
    const tile = screen.getByRole("article", { name: "Run #12" });
    expect(within(tile).getByRole("link", { name: "Mở run #12" })).toHaveAttribute("href", "/p/demo/runs/12");
    expect(within(tile).getByTestId("tile-project")).toHaveTextContent("demo");
    expect(within(tile).getByTestId("tile-title")).toHaveTextContent("Queue with dispatch and claim");
    expect(within(tile).getByTestId("run-state")).toHaveAttribute("data-status", "waiting");
    expect(within(tile).getByTestId("tile-waiting-you")).toHaveTextContent("Đang chờ bạn");
    expect(within(tile).getByText("laptop")).toBeInTheDocument();
    // View only: no Cancel, no message box.
    expect(within(tile).getAllByRole("button").map((button) => button.getAttribute("aria-label"))).toEqual(["Thôi xem run #12"]);
    expect(within(tile).queryByRole("textbox")).toBeNull();
    await userEvent.click(within(tile).getByRole("button", { name: "Thôi xem run #12" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it(`reads the trace from ${TAIL_EVENTS} events before last_seq, follows it live, and closes the stream when it leaves`, async () => {
    const { unmount, signals } = show(result(RUN));
    expect(FakeSource.made).toHaveLength(1);
    expect(FakeSource.made[0].url).toBe(`/v1/projects/demo/runs/12/stream?after=${RUN.last_seq - TAIL_EVENTS}`);
    const source = FakeSource.made[0];
    await act(async () => {
      source.open();
      source.send(say("Reading the plan."));
      source.send(run("pnpm test"));
      await new Promise((resolve) => setTimeout(resolve, 80));
    });
    const trace = screen.getByRole("log", { name: "Trace của run #12" });
    expect(trace).toHaveAttribute("aria-live", "off");
    const lines = within(trace).getAllByTestId("tile-line");
    expect(lines.map((line) => line.getAttribute("data-type"))).toEqual(["agent", "tool"]);
    expect(lines[0]).toHaveTextContent("Agent Reading the plan.");
    expect(lines[1]).toHaveTextContent("Bash pnpm test, đang chạy");
    expect(within(trace).getByTestId("tile-earlier")).toHaveTextContent("Các sự kiện trước đó nằm ở trang của run.");
    expect(screen.getByTestId("tile-stream")).toHaveAttribute("data-status", "live");
    expect(signals.snapshot()).toHaveLength(1);
    unmount();
    expect(source.closed).toBe(true);
    expect(signals.snapshot()).toHaveLength(0);
  });

  it("closes the stream for good once the run ended, and keeps the run's last state", async () => {
    const done = { ...RUN, state: "failed", finished_at: "2026-10-09T03:09:00Z", last_seq: 3 } as Run;
    seq = 0;
    const { update } = show(result(RUN));
    const source = FakeSource.made[0];
    await act(async () => {
      source.open();
      source.send(say("Giving up."));
      source.end("failed", 1);
      await new Promise((resolve) => setTimeout(resolve, 80));
    });
    expect(source.closed).toBe(true);
    expect(screen.queryByTestId("tile-stream")).toBeNull();
    update(result(done), null);
    expect(screen.getByTestId("run-state")).toHaveAttribute("data-status", "failed");
    expect(screen.getByTestId("tile-said")).toHaveTextContent("Run #12 giờ ở trạng thái Thất bại.");
    expect(FakeSource.made).toHaveLength(1);
  });

  it("says when the run cannot be read, and tries again on request", async () => {
    const refetch = vi.fn();
    show(result(undefined, { isError: true, refetch }), { decision: null });
    const tile = screen.getByRole("article", { name: "Run #12" });
    expect(within(tile).getByTestId("tile-error")).toHaveTextContent("Không đọc được run #12.");
    expect(FakeSource.made).toHaveLength(0);
    await userEvent.click(within(tile).getByRole("button", { name: "Thử lại" }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });
});
