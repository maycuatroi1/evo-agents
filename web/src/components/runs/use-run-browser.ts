"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";

import { ackFrame, type BrowserEvent, browserHello, type BrowserState, browserUrl, inputFrame, parseFrame, type ScreenHeader } from "./browser-view-model";
import { CLOSE_NORMAL, type CloseKind, closeKind } from "./terminal-model";

/**
 * The page's end of a run's live browser view: one websocket on the page's own origin, open while `enabled` (the tab
 * is shown and the run's browser is up). connecting: the CSRF value is read, the socket opens and says hello; live:
 * the hub answered with LINK, which says whether the worker's end is there (frames pass only while it is); closed:
 * `end` says why. A connection lost, or a hub going away or failing, is tried again a few times, waiting longer each
 * time, with `retrying` set meanwhile.
 *
 * Each SCREEN frame goes to `onScreen`, one after the other; its ACK goes back once it is drawn (or could not be), and
 * the worker sends the next frame after it. STATE gives the mode and the tabs; a notice it carries (a URL refused)
 * shows for a while, except the one the hub's copy of the last STATE may still hold when a socket opens.
 */
export type BrowserStatus = "idle" | "connecting" | "live" | "closed";
export type BrowserEnd = { code: number; reason: string; kind: CloseKind };

type View = { status: Exclude<BrowserStatus, "idle">; end: BrowserEnd | null; worker: boolean; state: BrowserState | null; retrying: boolean };

const START: View = { status: "connecting", end: null, worker: false, state: null, retrying: false };
/** Waits before each new try after a connection lost, then the view stays closed with Reconnect. */
const RETRY_MS = [1_000, 2_000, 4_000, 8_000, 15_000];
const RETRY_KINDS: ReadonlySet<CloseKind> = new Set(["lost", "shutdown", "unavailable"]);
/** How long a notice of the broker shows. */
export const NOTICE_MS = 8_000;

type Options = {
  project: string;
  runId: number;
  enabled: boolean;
  /** Draw a screen frame; the ACK waits for it. */
  onScreen: (header: ScreenHeader, jpeg: Uint8Array) => Promise<void>;
  /** Whether the visitor's sign-in is older than 12 hours now, which the hub refuses with 4403 like other refusals. */
  sessionOld: () => boolean;
};

export type RunBrowser = {
  status: BrowserStatus;
  end: BrowserEnd | null;
  /** The worker's end is connected, so frames pass. */
  worker: boolean;
  state: BrowserState | null;
  retrying: boolean;
  notice: string | null;
  /** An event of the person; false when it cannot go now (not connected, or the worker's end is not there). */
  send: (event: BrowserEvent) => boolean;
  /** Open the view again, after it closed. */
  reconnect: () => void;
};

export function useRunBrowser({ project, runId, enabled, onScreen, sessionOld }: Options): RunBrowser {
  const [session, setSession] = useState(0);
  const key = enabled ? session : -1;
  const [viewKey, setViewKey] = useState(key);
  const [view, setView] = useState<View>(START);
  const [notice, setNotice] = useState<string | null>(null);
  // A new connection starts from nothing: what the last one said is not what this one will.
  if (viewKey !== key) {
    setViewKey(key);
    setView(START);
  }
  const socketRef = useRef<WebSocket | null>(null);
  const workerRef = useRef(false);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const handlers = useRef({ onScreen, sessionOld });
  useLayoutEffect(() => {
    handlers.current = { onScreen, sessionOld };
  });

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let tries = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let current: WebSocket | null = null;

    const close = (end: BrowserEnd) => setView((view) => ({ ...view, status: "closed", end, worker: false, retrying: false }));
    const retry = () => {
      if (tries >= RETRY_MS.length) return false;
      const wait = RETRY_MS[tries];
      tries += 1;
      setView((view) => ({ ...view, status: "connecting", worker: false, retrying: true }));
      timer = setTimeout(() => void open(), wait);
      return true;
    };

    const open = async () => {
      let csrf: string;
      try {
        csrf = (await call(browserApi().GET("/v1/auth/web/csrf"))).csrf;
      } catch (error) {
        if (stopped) return;
        const signIn = isApiError(error) && error.kind === "unauthorized";
        if (!signIn && retry()) return;
        close({ code: 0, reason: "", kind: signIn ? "signIn" : "unavailable" });
        return;
      }
      if (stopped) return;
      const socket = new WebSocket(browserUrl(window.location, project, runId));
      socket.binaryType = "arraybuffer";
      current = socket;
      socketRef.current = socket;
      workerRef.current = false;
      let first = true; // the hub's copy of the last STATE comes first, its notice already said
      let drawing: Promise<unknown> = Promise.resolve();
      socket.onopen = () => socket.send(browserHello(csrf));
      socket.onmessage = (event) => {
        if (stopped || current !== socket) return;
        const frame = parseFrame(event.data);
        if (frame === null) return;
        if (frame.type === "link") {
          tries = 0;
          workerRef.current = frame.worker;
          setView((view) => ({ ...view, status: "live", end: null, worker: frame.worker, retrying: false }));
        } else if (frame.type === "state") {
          const said = first ? null : frame.state.notice;
          first = false;
          setView((view) => ({ ...view, state: frame.state }));
          if (said) {
            clearTimeout(noticeTimer.current);
            setNotice(said);
            noticeTimer.current = setTimeout(() => setNotice(null), NOTICE_MS);
          }
        } else {
          const { header, jpeg } = frame;
          drawing = drawing
            .then(() => handlers.current.onScreen(header, jpeg))
            .catch(() => undefined) // a frame that cannot be drawn is skipped: the next one replaces it
            .then(() => {
              if (!stopped && current === socket && socket.readyState === WebSocket.OPEN) socket.send(ackFrame(header.seq));
            });
        }
      };
      socket.onclose = (event) => {
        if (current !== socket) return;
        current = null;
        if (socketRef.current === socket) socketRef.current = null;
        workerRef.current = false;
        if (stopped) return;
        const kind = closeKind(event.code, { sessionOld: handlers.current.sessionOld() });
        if (RETRY_KINDS.has(kind) && retry()) return;
        close({ code: event.code, reason: event.reason, kind });
      };
    };

    void open();
    return () => {
      stopped = true;
      clearTimeout(timer);
      const socket = current;
      current = null;
      if (socketRef.current === socket) socketRef.current = null;
      workerRef.current = false;
      socket?.close(CLOSE_NORMAL, "the view closed");
    };
  }, [enabled, project, runId, session]);

  useEffect(() => () => clearTimeout(noticeTimer.current), []);

  const send = useCallback((event: BrowserEvent) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN || !workerRef.current) return false;
    socket.send(inputFrame(event));
    return true;
  }, []);

  const reconnect = useCallback(() => setSession((value) => value + 1), []);

  const shown = viewKey === key ? view : START;
  return {
    status: enabled ? shown.status : "idle",
    end: enabled ? shown.end : null,
    worker: enabled && shown.worker,
    state: shown.state,
    retrying: enabled && shown.retrying,
    notice: enabled ? notice : null,
    send,
    reconnect,
  };
}
