"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";

import {
  CLOSE_NORMAL,
  type CloseKind,
  closeKind,
  clampSize,
  helloMessage,
  inputFrames,
  outputPayload,
  resizeFrame,
  terminalUrl,
} from "./terminal-model";

/**
 * The browser's end of a run's terminal: one websocket on the page's own origin (the reverse proxy, or the web's own
 * /v1 forwarding, takes it to the API), opened by `connect` with the terminal's size.
 *
 * connecting: the CSRF value is read and the socket opens; waiting: the hello went out and the hub waits for the
 * worker's end (the daemon learns of it from its next heartbeat, within 15 seconds; a headless run first stops at
 * the end of the agent's turn); live: the first output came, so input now reaches the agent's TUI. The hub drops input
 * sent before the worker's end connects, so nothing typed is sent until then. closed: `end` says why.
 */
export type TerminalStatus = "idle" | "connecting" | "waiting" | "live" | "closed";
export type TerminalEnd = { code: number; reason: string; kind: CloseKind };

type Options = {
  project: string;
  runId: number;
  /** What the worker's terminal printed. */
  onOutput: (bytes: Uint8Array) => void;
  /** Whether the visitor's sign-in is older than 12 hours now, which the hub refuses with 4403 like other refusals. */
  sessionOld: () => boolean;
};

export type RunTerminal = {
  status: TerminalStatus;
  end: TerminalEnd | null;
  /** The size the hub last got, once the socket is open. */
  size: { cols: number; rows: number } | null;
  connect: (cols: number, rows: number) => void;
  disconnect: () => void;
  /** Keys typed or text pasted; false when the terminal does not take input yet. */
  send: (data: string | Uint8Array) => boolean;
  resize: (cols: number, rows: number) => void;
};

export function useRunTerminal({ project, runId, onOutput, sessionOld }: Options): RunTerminal {
  const [status, setStatus] = useState<TerminalStatus>("idle");
  const [end, setEnd] = useState<TerminalEnd | null>(null);
  const [size, setSize] = useState<{ cols: number; rows: number } | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const liveRef = useRef(false);
  const closedByUserRef = useRef(false);
  const sizeRef = useRef({ cols: 80, rows: 24 });
  const attemptRef = useRef(0);
  const handlers = useRef({ onOutput, sessionOld });
  useLayoutEffect(() => {
    handlers.current = { onOutput, sessionOld };
  });

  const finish = useCallback((next: TerminalEnd) => {
    liveRef.current = false;
    setEnd(next);
    setStatus("closed");
  }, []);

  const connect = useCallback(
    (cols: number, rows: number) => {
      socketRef.current?.close(CLOSE_NORMAL);
      socketRef.current = null;
      const attempt = ++attemptRef.current;
      liveRef.current = false;
      closedByUserRef.current = false;
      sizeRef.current = clampSize(cols, rows);
      setEnd(null);
      setSize(null);
      setStatus("connecting");
      void (async () => {
        let csrf: string;
        try {
          csrf = (await call(browserApi().GET("/v1/auth/web/csrf"))).csrf;
        } catch (error) {
          if (attempt !== attemptRef.current) return;
          const signIn = isApiError(error) && error.kind === "unauthorized";
          finish({ code: 0, reason: "", kind: signIn ? "signIn" : "unavailable" });
          return;
        }
        if (attempt !== attemptRef.current || closedByUserRef.current) return;
        const socket = new WebSocket(terminalUrl(window.location, project, runId));
        socket.binaryType = "arraybuffer";
        socketRef.current = socket;
        socket.onopen = () => {
          const { cols: c, rows: r } = sizeRef.current;
          socket.send(helloMessage(csrf, c, r));
          setSize(sizeRef.current);
          setStatus((current) => (current === "connecting" ? "waiting" : current));
        };
        socket.onmessage = (event) => {
          const bytes = outputPayload(event.data);
          if (bytes === null || socketRef.current !== socket) return;
          if (!liveRef.current) {
            liveRef.current = true;
            setStatus("live");
          }
          handlers.current.onOutput(bytes);
        };
        socket.onclose = (event) => {
          if (socketRef.current !== socket) return;
          socketRef.current = null;
          const kind = closeKind(event.code, { closedByUser: closedByUserRef.current, sessionOld: handlers.current.sessionOld() });
          finish({ code: event.code, reason: event.reason, kind });
        };
      })();
    },
    [finish, project, runId],
  );

  const disconnect = useCallback(() => {
    closedByUserRef.current = true;
    const socket = socketRef.current;
    if (socket && socket.readyState <= WebSocket.OPEN) {
      socket.close(CLOSE_NORMAL, "closed in the browser");
      return; // onclose reports it
    }
    attemptRef.current += 1; // a connect still reading the CSRF value stops there
    finish({ code: CLOSE_NORMAL, reason: "", kind: "closed" });
  }, [finish]);

  const send = useCallback((data: string | Uint8Array) => {
    const socket = socketRef.current;
    if (!liveRef.current || !socket || socket.readyState !== WebSocket.OPEN) return false;
    for (const frame of inputFrames(data)) socket.send(frame);
    return true;
  }, []);

  const resize = useCallback((cols: number, rows: number) => {
    const next = clampSize(cols, rows);
    if (next.cols === sizeRef.current.cols && next.rows === sizeRef.current.rows) return;
    sizeRef.current = next;
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(resizeFrame(next.cols, next.rows)); // before the worker's end connects, the hub keeps it as its first size
      setSize(next);
    }
  }, []);

  // Leaving the page closes the session; the agent's TUI goes on in tmux on the worker.
  useEffect(
    () => () => {
      attemptRef.current += 1;
      const socket = socketRef.current;
      socketRef.current = null;
      socket?.close(CLOSE_NORMAL, "the page closed");
    },
    [],
  );

  return { status, end, size, connect, disconnect, send, resize };
}
