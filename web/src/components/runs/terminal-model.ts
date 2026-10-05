import type { Run, RunState } from "./queries";
import type { RunViewer } from "./run-model";

/**
 * The browser's end of a run's web terminal (docs/workers.md, Terminal), as pure functions: who sees the tab, the
 * frames on the wire, and what a close code means. `evo_agents/hub/terminal.py` is the wire format on the hub's side.
 *
 * The first message is a text hello, `{"csrf": ..., "cols": ..., "rows": ...}`; every message after it is a binary
 * frame whose first byte is its type, as in ttyd: 0 input (keys typed, text pasted), 1 output (what the terminal
 * printed), 2 resize (columns, then rows, two unsigned 16-bit big-endian integers from 1 to 1000).
 */

/** `terminal.OPEN_STATES` of the hub: a browser opens the terminal of a run in one of these. */
export const TERMINAL_OPEN_STATES = ["leased", "running", "interactive"] as const satisfies readonly RunState[];
/** `terminal.SESSION_MAX_AGE`: a web session older than this opens no terminal; its owner signs in again. */
export const SESSION_MAX_AGE_MS = 12 * 60 * 60 * 1000;
/** `terminal.MAX_FRAME_BYTES`: a whole frame, its type byte included. */
export const MAX_FRAME_BYTES = 64 * 1024;
/** `terminal.MAX_SIZE`: columns or rows of a resize. */
export const MAX_SIZE = 1000;

export const FRAME_INPUT = 0;
export const FRAME_OUTPUT = 1;
export const FRAME_RESIZE = 2;

export function isOpenState(state: string): boolean {
  return (TERMINAL_OPEN_STATES as readonly string[]).includes(state);
}

// Who sees the tab.

export type TerminalWorker = { id: number; owner: string; allow_web_terminal: boolean };

/**
 * Whether the visitor may use the run's terminal, as the hub decides it when the socket opens: the run is theirs
 * (they dispatched it) and so is the worker holding it, which allows the web terminal. Null for anyone else, who
 * never sees the tab. `open` says whether the run's state lets a terminal open now.
 */
export function terminalAccess(
  run: Pick<Run, "state" | "dispatched_by" | "worker_id">,
  viewer: Pick<RunViewer, "login"> | null,
  worker: TerminalWorker | null | undefined,
): { open: boolean } | null {
  if (!viewer || viewer.login !== run.dispatched_by) return null;
  if (!worker || run.worker_id === null || worker.id !== run.worker_id) return null;
  if (worker.owner !== viewer.login || !worker.allow_web_terminal) return null;
  return { open: isOpenState(run.state) };
}

/** Whether a web session created at `createdAt` (ISO 8601) is too old to open a terminal at `now` (ms). */
export function sessionTooOld(createdAt: string | null | undefined, now: number | null): boolean {
  if (!createdAt || now === null) return false;
  const created = Date.parse(createdAt);
  return Number.isFinite(created) && now - created >= SESSION_MAX_AGE_MS;
}

/** Whole hours since `createdAt`, for the sign-in prompt. */
export function sessionHours(createdAt: string, now: number): number {
  return Math.max(0, Math.floor((now - Date.parse(createdAt)) / 3_600_000));
}

// The wire.

/** The terminal's websocket on the page's own origin: ws:// next to http://, wss:// next to https://. */
export function terminalUrl(location: Pick<Location, "protocol" | "host">, project: string, runId: number): string {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/v1/projects/${encodeURIComponent(project)}/runs/${runId}/terminal`;
}

/** A size as the hub takes it: whole numbers from 1 to 1000. */
export function clampSize(cols: number, rows: number): { cols: number; rows: number } {
  const clamp = (value: number) => Math.min(MAX_SIZE, Math.max(1, Math.floor(Number.isFinite(value) ? value : 1)));
  return { cols: clamp(cols), rows: clamp(rows) };
}

/** The first message: the session's CSRF value and the terminal's size. */
export function helloMessage(csrf: string, cols: number, rows: number): string {
  return JSON.stringify({ csrf, ...clampSize(cols, rows) });
}

const encoder = new TextEncoder();

/**
 * What the person typed or pasted, as input frames of at most MAX_FRAME_BYTES each: a long paste goes in parts, which
 * the PTY on the worker reads as one stream. Strings are sent as UTF-8.
 */
export function inputFrames(data: string | Uint8Array): Uint8Array[] {
  const bytes = typeof data === "string" ? encoder.encode(data) : data;
  const room = MAX_FRAME_BYTES - 1;
  const frames: Uint8Array[] = [];
  for (let start = 0; start < bytes.length; start += room) {
    const part = bytes.subarray(start, start + room);
    const frame = new Uint8Array(part.length + 1);
    frame[0] = FRAME_INPUT;
    frame.set(part, 1);
    frames.push(frame);
  }
  return frames;
}

/** A resize frame: type 2, then the columns and the rows as big-endian uint16. */
export function resizeFrame(cols: number, rows: number): Uint8Array {
  const size = clampSize(cols, rows);
  const frame = new Uint8Array(5);
  const view = new DataView(frame.buffer);
  frame[0] = FRAME_RESIZE;
  view.setUint16(1, size.cols);
  view.setUint16(3, size.rows);
  return frame;
}

/** The bytes of an output frame from the worker, or null for any other message. */
export function outputPayload(data: unknown): Uint8Array | null {
  if (!(data instanceof ArrayBuffer) || data.byteLength === 0) return null;
  const bytes = new Uint8Array(data);
  return bytes[0] === FRAME_OUTPUT ? bytes.subarray(1) : null;
}

// How a session ended.

export const CLOSE_NORMAL = 1000;
export const CLOSE_GOING_AWAY = 1001;
export const CLOSE_UNSUPPORTED = 1003;
export const CLOSE_ABNORMAL = 1006; // the browser's code for a connection lost without a close frame
export const CLOSE_TOO_BIG = 1009;
export const CLOSE_UNAVAILABLE = 1011;
export const CLOSE_UNAUTHENTICATED = 4401;
export const CLOSE_FORBIDDEN = 4403;
export const CLOSE_TIMEOUT = 4408;
export const CLOSE_BUSY = 4409;
export const CLOSE_UPGRADE = 4426;

export type CloseKind =
  | "closed" // the person pressed Disconnect
  | "ended" // the worker's end left, or the session ended there
  | "shutdown"
  | "protocol"
  | "lost"
  | "unavailable"
  | "signIn" // no live session (4401), or one older than 12 hours (4403)
  | "forbidden"
  | "timeout"
  | "busy"
  | "upgrade";

/**
 * What a close means to the person: `closedByUser` for the page's own Disconnect, `sessionOld` when the visitor's
 * sign-in is older than 12 hours, which the hub refuses with 4403 too.
 */
export function closeKind(code: number, { closedByUser = false, sessionOld = false } = {}): CloseKind {
  if (closedByUser) return "closed";
  switch (code) {
    case CLOSE_NORMAL:
      return "ended";
    case CLOSE_GOING_AWAY:
      return "shutdown";
    case CLOSE_UNSUPPORTED:
    case CLOSE_TOO_BIG:
      return "protocol";
    case CLOSE_UNAVAILABLE:
      return "unavailable";
    case CLOSE_UNAUTHENTICATED:
      return "signIn";
    case CLOSE_FORBIDDEN:
      return sessionOld ? "signIn" : "forbidden";
    case CLOSE_TIMEOUT:
      return "timeout";
    case CLOSE_BUSY:
      return "busy";
    case CLOSE_UPGRADE:
      return "upgrade";
    default:
      return "lost";
  }
}
