// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";

import type { RunEvent } from "./queries";
import { MAX_STREAM_ERRORS, POLL_MS, STALL_MS, useRunLog } from "./use-run-log";

const api = vi.hoisted(() => ({
  pages: [] as unknown[],
  seen: [] as string[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request.url);
        const page = api.pages.shift();
        if (!page) return Response.json({ run_id: 7, state: "running", last_seq: 0, events: [], more: false });
        return Response.json(page);
      },
    }),
}));

/** An EventSource the test drives: open, deliver events, fail, end. */
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
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event), lastEventId: String(event.seq) }));
  }
  fail(readyState: 0 | 2) {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }
  end(state: string, lastSeq: number) {
    for (const listener of this.listeners.get("end") ?? []) {
      listener(new MessageEvent("end", { data: JSON.stringify({ state, last_seq: lastSeq }) }));
    }
  }
}

const event = (seq: number, text = `line ${seq}`): RunEvent => ({
  seq,
  at: "2026-10-05T07:00:00Z",
  kind: "agent_message_chunk",
  body: { text },
  truncated: false,
});

function setup(knownLastSeq = 0, tail: { startAfter?: number; keep?: number } = {}) {
  return renderHook((props: { known: number; startAfter?: number }) =>
    useRunLog({
      project: "demo",
      runId: 7,
      knownLastSeq: props.known,
      describe: (move) => `${move.from}>${move.to}`,
      startAfter: props.startAfter,
      keep: tail.keep,
      openSource: (url) => new FakeSource(url) as unknown as EventSource,
    }),
  { initialProps: { known: knownLastSeq, startAfter: tail.startAfter } as { known: number; startAfter?: number } });
}

async function flush(ms = 60) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSource.made = [];
  api.pages = [];
  api.seen = [];
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useRunLog", () => {
  it("follows the stream from the start, and shows each event once", async () => {
    const { result } = setup();
    expect(FakeSource.made).toHaveLength(1);
    expect(FakeSource.made[0].url).toBe("/v1/projects/demo/runs/7/stream?after=0");
    expect(result.current.status).toBe("connecting");
    const source = FakeSource.made[0];
    act(() => source.open());
    expect(result.current.status).toBe("live");
    act(() => {
      source.send(event(1));
      source.send(event(2));
      source.send(event(2)); // a repeat after a reconnect
      source.send(event(1));
    });
    await flush();
    expect(result.current.lines.map((line) => line.seq)).toEqual([1, 2]);
    expect(result.current.lastSeq).toBe(2);
  });

  it("lets the browser reconnect by itself, then closes for good on end", async () => {
    const { result } = setup();
    const source = FakeSource.made[0];
    act(() => {
      source.open();
      source.send(event(1));
      source.fail(0); // the connection dropped; the browser reconnects with Last-Event-ID
    });
    expect(result.current.status).toBe("reconnecting");
    act(() => {
      source.open();
      source.send(event(2));
      source.send({ ...event(3), kind: "state", body: { from: "verifying", to: "review", actor: "worker" } });
      source.end("review", 3);
    });
    await flush();
    expect(result.current.status).toBe("ended");
    expect(result.current.endState).toBe("review");
    expect(result.current.moves.map((move) => move.to)).toEqual(["review"]);
    expect(source.closed).toBe(true);
    await flush(60_000);
    expect(FakeSource.made).toHaveLength(1); // never reconnects after end
    expect(api.seen).toHaveLength(0);
  });

  it("reads events after the last one when the stream fails for good, until the run is final", async () => {
    const { result } = setup();
    const source = FakeSource.made[0];
    act(() => {
      source.open();
      source.send(event(1));
    });
    api.pages = [
      { run_id: 7, state: "running", last_seq: 3, events: [event(1), event(2)], more: true },
      { run_id: 7, state: "running", last_seq: 3, events: [event(3)], more: false },
      { run_id: 7, state: "done", last_seq: 4, events: [event(4)], more: false },
    ];
    act(() => source.fail(2));
    expect(result.current.status).toBe("polling");
    expect(source.closed).toBe(true);
    await flush(10);
    expect(api.seen[0]).toContain("/v1/projects/demo/runs/7/events?after=1&limit=1000");
    await flush(10);
    expect(api.seen[1]).toContain("after=2");
    await flush(POLL_MS + 10);
    expect(api.seen[2]).toContain("after=3");
    await flush();
    expect(result.current.lines.map((line) => line.seq)).toEqual([1, 2, 3, 4]);
    expect(result.current.status).toBe("ended");
    expect(result.current.endState).toBe("done");
  });

  it(`reads events after ${MAX_STREAM_ERRORS} errors in a row, and tries the stream again later`, async () => {
    const { result } = setup();
    const source = FakeSource.made[0];
    act(() => {
      for (let tries = 0; tries < MAX_STREAM_ERRORS; tries += 1) source.fail(0);
    });
    expect(result.current.status).toBe("polling");
    await flush(30_000);
    expect(FakeSource.made).toHaveLength(2);
    expect(FakeSource.made[1].url).toBe("/v1/projects/demo/runs/7/stream?after=0");
    act(() => FakeSource.made[1].open());
    expect(result.current.status).toBe("live");
  });

  it("reads only the tail when told where to start, by stream and by reads alike, and keeps the latest events", async () => {
    const { result, rerender } = setup(250, { startAfter: 50, keep: 3 });
    expect(FakeSource.made[0].url).toBe("/v1/projects/demo/runs/7/stream?after=50");
    const source = FakeSource.made[0];
    act(() => {
      source.open();
      source.send(event(50)); // at or before the start: not read
      for (const seq of [51, 52, 53, 54]) source.send(event(seq));
      source.send({ ...event(55), kind: "state", body: { from: "running", to: "verifying", actor: "worker" } });
    });
    await flush();
    expect(result.current.events.map((item) => item.seq)).toEqual([53, 54, 55]);
    expect(result.current.lines.map((line) => line.seq)).toEqual([53, 54, 55]);
    expect(result.current.lastSeq).toBe(55);
    // A later start does not move the stream already open.
    rerender({ known: 260, startAfter: 200 });
    expect(FakeSource.made).toHaveLength(1);

    // The fallback reads on from the last event kept, never from the run's first.
    api.pages = [{ run_id: 7, state: "running", last_seq: 56, events: [event(56)], more: false }];
    act(() => source.fail(2));
    await flush(10);
    expect(api.seen[0]).toContain("events?after=55&limit=1000");
    await flush();
    expect(result.current.events.map((item) => item.seq)).toEqual([54, 55, 56]);
  });

  it("closes the stream and stops reading when the page leaves", async () => {
    const { unmount } = setup();
    const source = FakeSource.made[0];
    act(() => source.fail(2));
    await flush(10);
    const reads = api.seen.length;
    unmount();
    expect(source.closed).toBe(true);
    await flush(60_000);
    expect(api.seen).toHaveLength(reads);
    expect(FakeSource.made).toHaveLength(1);
  });

  it("reads events instead when the stream stays behind the run for 10 seconds, and does not try it again", async () => {
    const { result, rerender } = setup();
    act(() => FakeSource.made[0].open());
    rerender({ known: 5 });
    await flush(STALL_MS + 2_100);
    expect(result.current.status).toBe("polling");
    await flush(60_000);
    expect(FakeSource.made).toHaveLength(1);
  });
});
