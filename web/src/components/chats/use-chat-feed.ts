"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import type { LiveSignal } from "@/components/live/live-model";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { chatEventsAfter, chatKeys, chatStreamPath, markChatRead, olderMessages, threadMessages } from "@/lib/chat-queries";
import type { ChatEvent, ChatPage, ChatProgress, ChatSummary, GroupMessage } from "@/lib/chats";
import { LOGIN_PATH } from "@/lib/config";

import { applyEvents, type Feed, feedOf, readInto, withMessage, withProgress, withReactions } from "./feed-model";

/**
 * A chat's messages in the browser, live (docs/hub.md, Group chats).
 *
 * The page follows `GET /v1/chats/{id}/stream` with an EventSource from the chat's last event when the page was read:
 * each event's id is its seq, so a dropped connection comes back by itself with `Last-Event-ID`, and an event at or
 * before the last one applied is a repeat. The stream says `end` once the visitor may no longer read the chat (taken
 * out of it, or a grant that no longer clears its label); the page stops then and says why. It also sends `progress`,
 * the progress of the agents' runs under the messages they took, at once and each time it changed, which applies after
 * the events that came with it. When the stream fails for good (the browser gives up, or it errs three times without
 * opening), the page reads `events?after=` (with that progress) every 2 seconds instead and tries the stream again
 * every 30.
 *
 * While the page is in view, what it shows is read: the page tells the hub the seq of the last event it applied, so
 * the list's unread count (and the sidebar's) drops to what came since.
 */
export type FeedStatus = "connecting" | "live" | "reconnecting" | "polling" | "ended";

const MAX_STREAM_ERRORS = 3;
export const POLL_MS = 2_000;
const STREAM_RETRY_MS = 30_000;
const FLUSH_MS = 50;
const READ_DELAY_MS = 800;
/** `EventSource.CLOSED`: the browser gave up and will not reconnect. */
const CLOSED = 2;

export type ChatFeed = {
  feed: Feed;
  status: FeedStatus;
  /** Why the stream ended: the visitor may no longer read the chat. */
  ended: string | null;
  signal: LiveSignal | null;
  loadOlder: () => Promise<void>;
  loadThread: (root: number) => Promise<void>;
  /** A message a link points at: held already, or read with its thread; null when the visitor cannot read it. */
  locate: (id: number) => Promise<GroupMessage | null>;
  /** A message the visitor sent, or reacted to, as the hub answered it. */
  sent: (message: GroupMessage) => void;
  reactedTo: (message: GroupMessage) => void;
};

function parse<T>(data: string): T | null {
  try {
    return JSON.parse(data) as T;
  } catch {
    return null;
  }
}

const defaultSource = (url: string) => new EventSource(url);

export function useChatFeed({
  chatId,
  page,
  me,
  onMembers,
  openSource = defaultSource,
}: {
  chatId: number;
  page: ChatPage;
  me: string | null;
  /** A member was added or removed: the chat's people and agents are read again. */
  onMembers: () => void;
  openSource?: (url: string) => EventSource;
}): ChatFeed {
  const queryClient = useQueryClient();
  const [feed, setFeed] = useState<Feed>(() => feedOf(page));
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const [ended, setEnded] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState(0);
  const [retryAt, setRetryAt] = useState<number | null>(null);
  const [pollFailing, setPollFailing] = useState(false);
  // The feed itself: every change goes through it, then to the state that renders it, so changes that arrive before a
  // render build on one another.
  const feedRef = useRef(feed);
  const live = useRef({ me, onMembers, openSource });
  useEffect(() => {
    live.current = { me, onMembers, openSource };
  });

  const apply = useCallback((events: readonly ChatEvent[], progress: readonly ChatProgress[] | null = null) => {
    if (!events.length && !progress) return;
    const result = applyEvents(feedRef.current, events, live.current.me);
    const next = progress ? withProgress(result.feed, progress) : result.feed;
    feedRef.current = next;
    setFeed(next);
    setUpdatedAt(Date.now());
    if (result.members) live.current.onMembers();
  }, []);

  useEffect(() => {
    let disposed = false;
    let source: EventSource | null = null;
    let errors = 0;
    let polling = false;
    let pollTimer: ReturnType<typeof setTimeout> | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let pending: ChatEvent[] = [];
    let pendingProgress: ChatProgress[] | null = null; // the latest the stream sent, applied after the events before it
    let abort: AbortController | null = null;

    const flush = () => {
      flushTimer = null;
      const batch = pending;
      const progress = pendingProgress;
      pending = [];
      pendingProgress = null;
      if (!disposed) apply(batch, progress);
    };
    const closeStream = () => {
      if (!source) return;
      source.onopen = null;
      source.onmessage = null;
      source.onerror = null;
      source.close();
      source = null;
    };
    const stopPolling = () => {
      polling = false;
      if (pollTimer) clearTimeout(pollTimer);
      if (retryTimer) clearTimeout(retryTimer);
      pollTimer = retryTimer = null;
      abort?.abort();
      setRetryAt(null);
      setPollFailing(false);
    };
    const finish = (reason: string) => {
      closeStream();
      stopPolling();
      if (flushTimer) {
        clearTimeout(flushTimer);
        flush();
      }
      setEnded(reason);
      setStatus("ended");
    };

    const poll = async () => {
      pollTimer = null;
      const controller = new AbortController();
      abort = controller;
      try {
        const answer = await chatEventsAfter(browserApi(), chatId, feedRef.current.lastSeq, controller.signal);
        if (disposed || controller.signal.aborted) return;
        setPollFailing(false);
        apply(answer.events, answer.progress ?? null);
        setUpdatedAt(Date.now());
        // More to read goes on at once, unless none of it was for this reader (labels it does not clear): then wait.
        if (polling) pollTimer = setTimeout(() => void poll(), answer.more && answer.events.length ? 0 : POLL_MS);
      } catch (error) {
        if (disposed || controller.signal.aborted) return;
        if (isApiError(error) && error.kind === "unauthorized") return window.location.assign(LOGIN_PATH);
        if (isApiError(error) && (error.kind === "forbidden" || error.kind === "not_found")) return finish(error.info.message);
        setPollFailing(true);
        if (polling) pollTimer = setTimeout(() => void poll(), POLL_MS * 2);
      }
    };
    const startPolling = () => {
      closeStream();
      if (!polling) {
        polling = true;
        setStatus("polling");
        void poll();
      }
      setRetryAt(Date.now() + STREAM_RETRY_MS);
      if (retryTimer) clearTimeout(retryTimer);
      retryTimer = setTimeout(() => {
        retryTimer = null;
        if (!disposed && polling) openStream();
      }, STREAM_RETRY_MS);
    };

    function openStream() {
      closeStream();
      const opened = live.current.openSource(chatStreamPath(chatId, feedRef.current.lastSeq));
      source = opened;
      errors = 0;
      opened.onopen = () => {
        if (disposed || source !== opened) return;
        errors = 0;
        stopPolling();
        setUpdatedAt(Date.now());
        setStatus("live");
      };
      opened.onmessage = (message: MessageEvent<string>) => {
        const event = parse<ChatEvent>(message.data);
        if (!event || typeof event.seq !== "number") return;
        pending.push(event);
        flushTimer ??= setTimeout(flush, FLUSH_MS);
      };
      opened.addEventListener("progress", (message) => {
        if (disposed || source !== opened) return;
        const body = parse<{ progress?: ChatProgress[] }>((message as MessageEvent<string>).data);
        if (!Array.isArray(body?.progress)) return;
        pendingProgress = body.progress;
        flushTimer ??= setTimeout(flush, FLUSH_MS);
      });
      opened.addEventListener("end", (message) => {
        if (disposed || source !== opened) return;
        const body = parse<{ reason?: string }>((message as MessageEvent<string>).data);
        finish(body?.reason ?? "");
      });
      opened.onerror = () => {
        if (disposed || source !== opened) return;
        errors += 1;
        if (opened.readyState === CLOSED || errors >= MAX_STREAM_ERRORS) startPolling();
        else if (!polling) setStatus("reconnecting");
      };
    }

    openStream();
    return () => {
      disposed = true;
      if (flushTimer) clearTimeout(flushTimer);
      closeStream();
      stopPolling();
    };
  }, [chatId, apply]);

  // What the page shows while it is in view is read: tell the hub, and drop the chat's unread count in the list.
  const lastSeq = feed.lastSeq;
  const told = useRef(page.chat.last_read_seq);
  useEffect(() => {
    if (ended) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tell = () => {
      if (document.visibilityState !== "visible" || feedRef.current.lastSeq <= told.current) return;
      const seq = feedRef.current.lastSeq;
      told.current = seq;
      queryClient.setQueryData<ChatSummary[]>(chatKeys.list, (list) => list?.map((chat) => (chat.id === chatId ? { ...chat, unread: 0 } : chat)));
      void markChatRead(browserApi(), chatId, seq)
        .then(() => queryClient.invalidateQueries({ queryKey: chatKeys.list }))
        .catch(() => {
          told.current = Math.min(told.current, seq - 1); // tried again with the next event or the next view
        });
    };
    const later = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(tell, READ_DELAY_MS);
    };
    later();
    document.addEventListener("visibilitychange", later);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", later);
    };
  }, [chatId, lastSeq, ended, queryClient]);

  const loadOlder = useCallback(async () => {
    const before = feedRef.current.oldestSeq;
    if (before === null) return;
    const answer = await olderMessages(browserApi(), chatId, before);
    feedRef.current = readInto(feedRef.current, answer, "older");
    setFeed(feedRef.current);
  }, [chatId]);

  const loadThread = useCallback(
    async (root: number) => {
      const answer = await threadMessages(browserApi(), chatId, root);
      feedRef.current = readInto(feedRef.current, answer, "thread");
      setFeed(feedRef.current);
    },
    [chatId],
  );

  const locate = useCallback(
    async (id: number) => {
      const held = feedRef.current.entries.get(id);
      if (held) return held.message;
      try {
        const answer = await threadMessages(browserApi(), chatId, id);
        feedRef.current = readInto(feedRef.current, answer, "thread");
        setFeed(feedRef.current);
        return answer.messages.find((message) => message.id === id) ?? null;
      } catch {
        return null;
      }
    },
    [chatId],
  );

  const sent = useCallback((message: GroupMessage) => {
    feedRef.current = withMessage(feedRef.current, message);
    setFeed(feedRef.current);
  }, []);

  const reactedTo = useCallback((message: GroupMessage) => {
    feedRef.current = withReactions(feedRef.current, message);
    setFeed(feedRef.current);
  }, []);

  const signal = feedSignal(status, updatedAt, retryAt, pollFailing);
  return { feed, status, ended, signal, loadOlder, loadThread, locate, sent, reactedTo };
}

/** The feed as a source of the top bar's LiveIndicator, as a run's log is one; null before it opens and once it ended. */
export function feedSignal(status: FeedStatus, updatedAt: number, retryAt: number | null, pollFailing: boolean): LiveSignal | null {
  const base = { transport: "stream" as const, updatedAt, offline: false, paused: false, retryAt: null, pollMs: null };
  switch (status) {
    case "live":
      return { ...base, failing: false, degraded: false };
    case "reconnecting":
      return { ...base, failing: true, degraded: false };
    case "polling":
      return pollFailing
        ? { ...base, failing: true, degraded: false, pollMs: POLL_MS }
        : { ...base, failing: false, degraded: true, retryAt, pollMs: POLL_MS };
    default:
      return null;
  }
}
