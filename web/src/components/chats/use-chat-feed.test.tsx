// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { Chat, ChatAuthor, ChatEvent, ChatPage, ChatProgress, GroupMessage } from "@/lib/chats";

import { mainMessages, replyCount } from "./feed-model";
import { POLL_MS, useChatFeed } from "./use-chat-feed";

const api = vi.hoisted(() => ({ seen: [] as { url: string; method: string; body: string }[], events: [] as unknown[] }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push({ url: request.url, method: request.method, body: await request.clone().text() });
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (request.url.includes("/read")) return Response.json({ last_read_seq: 9, unread: 0 });
        const page = api.events.shift();
        return Response.json(page ?? { events: [], last_seq: 0, more: false });
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
  send(event: ChatEvent) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(event), lastEventId: String(event.seq) }));
  }
  fail(readyState: 0 | 2) {
    this.readyState = readyState;
    this.onerror?.(new Event("error"));
  }
  progress(progress: ChatProgress[]) {
    for (const listener of this.listeners.get("progress") ?? []) listener(new MessageEvent("progress", { data: JSON.stringify({ progress }) }));
  }
  end(reason: string) {
    for (const listener of this.listeners.get("end") ?? []) listener(new MessageEvent("end", { data: JSON.stringify({ reason, last_seq: 4 }) }));
  }
}

const OCTO: ChatAuthor = { kind: "person", login: "octo", name: "octo", deleted: false };

function message(id: number, seq: number, overrides: Partial<GroupMessage> = {}): GroupMessage {
  return { id, chat_id: 3, seq, thread_root_id: null, reply_count: 0, author: OCTO, run_id: null, body: `m${id}`, mentions: [], reactions: [], label: [], created_at: "2026-10-14T08:00:00Z", ...overrides };
}

const PAGE: ChatPage = { chat: { id: 3, title: "Release", last_seq: 4, last_read_seq: 2 } as Chat, messages: [message(1, 1), message(2, 4)], more: false };

function setup(onMembers = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => useChatFeed({ chatId: 3, page: PAGE, me: "octo", onMembers, openSource: (url) => new FakeSource(url) as unknown as EventSource }), { wrapper });
  return { ...hook, onMembers };
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSource.made = [];
  api.seen = [];
  api.events = [];
});

afterEach(() => {
  vi.useRealTimers();
});

describe("useChatFeed", () => {
  it("follows the stream from the chat's last event, applies what it sends once, and tells the hub what was read", async () => {
    const { result, onMembers } = setup();
    const [source] = FakeSource.made;
    expect(source.url).toBe("/v1/chats/3/stream?after=4&progress=true");
    expect(result.current.status).toBe("connecting");
    act(() => source.open());
    expect(result.current.status).toBe("live");
    expect(result.current.signal).toMatchObject({ transport: "stream", failing: false });
    act(() => {
      source.send({ seq: 5, kind: "message", created_at: "2026-10-14T08:01:00Z", message: message(7, 5, { thread_root_id: 2 }) } as ChatEvent);
      source.send({ seq: 5, kind: "message", created_at: "2026-10-14T08:01:00Z", message: message(7, 5, { thread_root_id: 2 }) } as ChatEvent);
      source.send({ seq: 6, kind: "member_added", created_at: "2026-10-14T08:02:00Z", member: { member: OCTO, by: "octo" } } as ChatEvent);
      vi.advanceTimersByTime(60);
    });
    expect(replyCount(result.current.feed, 2)).toBe(1);
    expect(mainMessages(result.current.feed).map((item) => item.id)).toEqual([1, 2]);
    expect(onMembers).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(900);
    });
    const read = api.seen.find((call) => call.url.endsWith("/v1/chats/3/read"));
    expect(JSON.parse(read?.body ?? "{}")).toEqual({ seq: 6 });
  });

  it("reads events instead once the stream fails for good, and stops with the hub's reason when the stream ends", async () => {
    const { result } = setup();
    const [source] = FakeSource.made;
    api.events.push({ events: [{ seq: 5, kind: "message", created_at: "2026-10-14T08:01:00Z", message: message(8, 5) }], last_seq: 5, more: false });
    await act(async () => {
      source.fail(2);
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.status).toBe("polling");
    expect(api.seen.some((call) => call.url.includes("/v1/chats/3/events?after=4&progress=true"))).toBe(true);
    expect(mainMessages(result.current.feed).map((item) => item.id)).toEqual([1, 2, 8]);
    expect(result.current.signal).toMatchObject({ degraded: true, pollMs: POLL_MS });
    // 30 seconds on, the stream is tried again, from the last event read.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    const retried = FakeSource.made.at(-1)!;
    expect(retried.url).toBe("/v1/chats/3/stream?after=5&progress=true");
    act(() => retried.open());
    expect(result.current.status).toBe("live");
    act(() => retried.end("you are not in chat 3"));
    expect(result.current.status).toBe("ended");
    expect(result.current.ended).toBe("you are not in chat 3");
    expect(result.current.signal).toBeNull();
    expect(retried.closed).toBe(true);
  });

  it("applies the stream's progress after the events that came before it, and a poll's with its events", async () => {
    const { result } = setup();
    const [source] = FakeSource.made;
    act(() => source.open());
    const working: ChatProgress = { message_id: 9, run_id: 21, handle: "reviewer", step: "waiting", worker: "studio", repo: null, since: "2026-10-14T08:01:00Z" };
    act(() => {
      source.send({ seq: 5, kind: "message", created_at: "2026-10-14T08:01:00Z", message: message(9, 5) } as ChatEvent);
      source.progress([working]);
      vi.advanceTimersByTime(60);
    });
    expect(result.current.feed.entries.get(9)?.message.progress).toEqual([working]);
    act(() => {
      source.progress([]);
      vi.advanceTimersByTime(60);
    });
    expect(result.current.feed.entries.get(9)?.message.progress).toEqual([]);
    api.events.push({ events: [], last_seq: 5, more: false, progress: [{ ...working, step: "opening", repo: "evo-agents" }] });
    await act(async () => {
      source.fail(2);
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(result.current.feed.entries.get(9)?.message.progress?.map((item) => item.repo)).toEqual(["evo-agents"]);
  });
});
