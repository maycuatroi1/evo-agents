/**
 * The live view of a run's browser, as `evo_agents/hub/browser_view.py` frames it: one binary websocket message per
 * frame, its first byte the frame's type. The viewer sends INPUT (an event of the person) and ACK (a screen frame
 * drawn); it reads SCREEN (a JPEG of the tab watched, after a JSON header), STATE (the mode and the tabs) and LINK
 * (whether the worker's end is there). Its first message is the hello, a text message with the session's CSRF value.
 */

export const FRAME_INPUT = 0;
export const FRAME_SCREEN = 1;
export const FRAME_STATE = 2;
export const FRAME_ACK = 3;
export const FRAME_LINK = 5;
/** A whole frame, its type byte included. */
export const MAX_FRAME_BYTES = 512 * 1024;
/** CSS pixels of the page, either way. */
export const MAX_COORDINATE = 100_000;
/** UTF-16 units of pasted text per frame: even escaped as \uXXXX (6 bytes a unit) one stays under MAX_FRAME_BYTES. */
export const PASTE_CHUNK = 60_000;
const MAX_URL_CHARS = 8192;
const MAX_NAME_CHARS = 64;
const MAX_KEY_TEXT_CHARS = 16;

export type BrowserMode = "agent" | "human";
export type BrowserTab = { id: string; url: string; title: string };
/** What the worker's broker says of the browser; `notice` once, when it refused something the person asked. */
export type BrowserState = { mode: BrowserMode; tabs: BrowserTab[]; active: string | null; notice: string | null };
/** The page's size in CSS pixels, the offset of its top and its zoom, as Chromium's screencast measures them. */
export type ScreenHeader = { seq: number; width: number; height: number; top: number; scale: number };

export type Frame =
  | { type: "screen"; header: ScreenHeader; jpeg: Uint8Array }
  | { type: "state"; state: BrowserState }
  | { type: "link"; worker: boolean };

export type MouseButton = "none" | "left" | "middle" | "right" | "back" | "forward";
export type KeyInput = { type: "key"; action: "down" | "up"; key: string; code: string; keyCode: number; text: string; modifiers: number };
export type BrowserEvent =
  | { type: "mouse"; action: "down" | "up" | "move"; x: number; y: number; button: MouseButton; buttons: number; clicks: number; modifiers: number }
  | { type: "wheel"; x: number; y: number; dx: number; dy: number; modifiers: number }
  | KeyInput
  | { type: "text" | "paste"; text: string }
  | { type: "back" | "forward" | "reload" }
  | { type: "tab"; id: string }
  | { type: "open"; url: string };

export function browserUrl(location: Pick<Location, "protocol" | "host">, project: string, runId: number): string {
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${location.host}/v1/projects/${encodeURIComponent(project)}/runs/${runId}/browser`;
}

export function browserHello(csrf: string): string {
  return JSON.stringify({ csrf });
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function message(kind: number, body: object): Uint8Array {
  const payload = encoder.encode(JSON.stringify(body));
  const frame = new Uint8Array(payload.length + 1);
  frame[0] = kind;
  frame.set(payload, 1);
  return frame;
}

export function inputFrame(event: BrowserEvent): Uint8Array {
  return message(FRAME_INPUT, event);
}

export function ackFrame(seq: number): Uint8Array {
  return message(FRAME_ACK, { seq });
}

function json(bytes: Uint8Array): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(decoder.decode(bytes));
    return value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

function screenOf(payload: Uint8Array): Frame | null {
  if (payload.length < 4) return null;
  const length = new DataView(payload.buffer, payload.byteOffset, payload.byteLength).getUint32(0);
  if (4 + length > payload.length) return null;
  const header = json(payload.subarray(4, 4 + length));
  if (!header) return null;
  const { seq, width, height, top = 0, scale = 1 } = header;
  if (!finite(seq) || !finite(width) || !finite(height) || !finite(top) || !finite(scale) || width <= 0 || height <= 0) return null;
  return { type: "screen", header: { seq, width, height, top, scale }, jpeg: payload.subarray(4 + length) };
}

function tabOf(value: unknown): BrowserTab | null {
  if (value === null || typeof value !== "object") return null;
  const { id, url, title } = value as Record<string, unknown>;
  if (typeof id !== "string" || !id) return null;
  return { id, url: typeof url === "string" ? url : "", title: typeof title === "string" ? title : "" };
}

function stateOf(payload: Uint8Array): Frame | null {
  const body = json(payload);
  if (!body || (body.mode !== "agent" && body.mode !== "human")) return null;
  const tabs = Array.isArray(body.tabs) ? body.tabs.map(tabOf).filter((tab): tab is BrowserTab => tab !== null) : [];
  const active = typeof body.active === "string" && tabs.some((tab) => tab.id === body.active) ? body.active : (tabs[0]?.id ?? null);
  const notice = typeof body.notice === "string" && body.notice ? body.notice : null;
  return { type: "state", state: { mode: body.mode, tabs, active, notice } };
}

/** A frame from the hub; null for anything else (a text message, a type the viewer does not read, a broken body). */
export function parseFrame(data: unknown): Frame | null {
  // A Uint8Array is copied, so the picture's bytes sit in an ArrayBuffer of their own (a Blob takes no shared memory).
  const bytes = data instanceof ArrayBuffer ? new Uint8Array(data) : data instanceof Uint8Array ? new Uint8Array(data) : null;
  if (!bytes || bytes.length === 0 || bytes.length > MAX_FRAME_BYTES) return null;
  const payload = bytes.subarray(1);
  switch (bytes[0]) {
    case FRAME_SCREEN:
      return screenOf(payload);
    case FRAME_STATE:
      return stateOf(payload);
    case FRAME_LINK: {
      const body = json(payload);
      return body && typeof body.worker === "boolean" ? { type: "link", worker: body.worker } : null;
    }
    default:
      return null;
  }
}

/** Pasted text in pieces of at most `size` UTF-16 units, never splitting a character made of two. */
export function pasteChunks(text: string, size = PASTE_CHUNK): string[] {
  const chunks: string[] = [];
  let start = 0;
  while (start < text.length) {
    let end = Math.min(start + size, text.length);
    const last = text.charCodeAt(end - 1);
    if (end < text.length && last >= 0xd800 && last <= 0xdbff) end -= 1; // a high surrogate waits for its low half
    chunks.push(text.slice(start, end));
    start = end;
  }
  return chunks;
}

/** The URL the box opens: what the person typed, trimmed; the broker adds https to one without a scheme. */
export function openUrl(typed: string): string | null {
  const url = typed.trim();
  return url && url.length <= MAX_URL_CHARS ? url : null;
}

export type Box = { left: number; top: number; width: number; height: number };

/** Where a picture of `width` by `height` lies in `box` when it is drawn as large as fits, centered (object-fit contain). */
export function fitted(box: Box, width: number, height: number): Box {
  if (width <= 0 || height <= 0 || box.width <= 0 || box.height <= 0) return { ...box, width: 0, height: 0 };
  const scale = Math.min(box.width / width, box.height / height);
  const w = width * scale;
  const h = height * scale;
  return { left: box.left + (box.width - w) / 2, top: box.top + (box.height - h) / 2, width: w, height: h };
}

const clamp = (value: number, low: number, high: number) => Math.min(Math.max(value, low), high);

/**
 * The page's point under a point of the window: `shown` is where the picture lies on screen, `header` the page it
 * shows. Null outside the picture unless `clampToPage` (a drag that left it keeps its last edge).
 */
export function toPage(x: number, y: number, shown: Box, header: Pick<ScreenHeader, "width" | "height">, clampToPage = false): { x: number; y: number } | null {
  if (shown.width <= 0 || shown.height <= 0) return null;
  const rx = (x - shown.left) / shown.width;
  const ry = (y - shown.top) / shown.height;
  if (!clampToPage && (rx < 0 || rx > 1 || ry < 0 || ry > 1)) return null;
  const round = (value: number) => Math.round(value * 100) / 100;
  return {
    x: round(clamp(clamp(rx, 0, 1) * header.width, -MAX_COORDINATE, MAX_COORDINATE)),
    y: round(clamp(clamp(ry, 0, 1) * header.height, -MAX_COORDINATE, MAX_COORDINATE)),
  };
}

type ModifierKeys = Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">;

/**
 * The broker's modifier bits (Alt 1, Ctrl 2, Meta 4, Shift 8). The run's Chromium runs on Linux, where shortcuts use
 * Ctrl: on an Apple device Command is sent as Ctrl, so Command C copies there as it does here.
 */
export function modifiersOf(event: ModifierKeys, apple: boolean): number {
  const ctrl = event.ctrlKey || (apple && event.metaKey);
  return (event.altKey ? 1 : 0) | (ctrl ? 2 : 0) | (!apple && event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
}

const ALT = 1;
const CTRL = 2;
const META = 4;
const SHIFT = 8;

/** Keys that type nothing, with the Windows key code Chromium's Input.dispatchKeyEvent wants; Enter types "\r". */
const NAMED_KEYS: Record<string, number> = {
  Backspace: 8,
  Tab: 9,
  Enter: 13,
  Shift: 16,
  Control: 17,
  Alt: 18,
  Pause: 19,
  CapsLock: 20,
  Escape: 27,
  PageUp: 33,
  PageDown: 34,
  End: 35,
  Home: 36,
  ArrowLeft: 37,
  ArrowUp: 38,
  ArrowRight: 39,
  ArrowDown: 40,
  Insert: 45,
  Delete: 46,
  Meta: 91,
  ContextMenu: 93,
  ...Object.fromEntries(Array.from({ length: 12 }, (_, index) => [`F${index + 1}`, 112 + index])),
};

/** A printable key's code when the browser gives none: the letter's or digit's capital, or the code's. */
function keyCodeOf(key: string, code: string, given: number): number {
  if (given > 0 && given <= 255) return given;
  if (/^Key[A-Z]$/.test(code)) return code.charCodeAt(3);
  if (/^Digit[0-9]$/.test(code)) return code.charCodeAt(5);
  if (code === "Space") return 32;
  const upper = key.toUpperCase();
  return /^[A-Z0-9]$/.test(upper) ? upper.charCodeAt(0) : 0;
}

type KeyLike = ModifierKeys & Pick<KeyboardEvent, "key" | "code" | "keyCode" | "isComposing">;

/** Whether a key press is the paste shortcut, which the viewer leaves to the paste event so the text arrives as text. */
export function isPasteKey(event: KeyLike, apple: boolean): boolean {
  const modifiers = modifiersOf(event, apple);
  return (event.key === "v" || event.key === "V") && (modifiers & CTRL) !== 0 && (modifiers & (ALT | META)) === 0;
}

/**
 * The key event to send for a key pressed or let go; null for one the page leaves to the text box: a key an input
 * method takes (Telex and VNI compose there, and a phone's keyboard says 229 or Unidentified), a dead key, and the
 * paste shortcut. A key that types sends its text, Enter "\r"; with Ctrl (Command on an Apple device) or Alt it sends
 * no text, as a shortcut. The Option key of a Mac and AltGr elsewhere type characters, which go as text.
 */
export function keyEventOf(event: KeyLike, action: "down" | "up", apple: boolean): KeyInput | null {
  const { key, code } = event;
  if (event.isComposing || event.keyCode === 229 || key === "Process" || key === "Dead" || key === "Unidentified" || !key) return null;
  if (action === "down" && isPasteKey(event, apple)) return null;
  const modifiers = modifiersOf(event, apple);
  const base = { type: "key" as const, action, key: key.slice(0, MAX_NAME_CHARS), code: code.slice(0, MAX_NAME_CHARS) };
  const named = NAMED_KEYS[key];
  if (named !== undefined) {
    const text = key === "Enter" && action === "down" ? "\r" : "";
    return { ...base, keyCode: named, text, modifiers };
  }
  const printable = [...key].length === 1;
  if (!printable) return { ...base, keyCode: keyCodeOf(key, code, event.keyCode), text: "", modifiers };
  const keyCode = keyCodeOf(key, code, event.keyCode);
  const altGraph = !apple && event.ctrlKey && event.altKey; // AltGr reads as Ctrl and Alt on Windows
  const option = apple && event.altKey && !(modifiers & CTRL); // a Mac's Option types a character of its own
  if (altGraph || option) return { ...base, keyCode, text: action === "down" ? key.slice(0, MAX_KEY_TEXT_CHARS) : "", modifiers: modifiers & SHIFT };
  if (modifiers & (CTRL | META | ALT)) return { ...base, keyCode, text: "", modifiers };
  return { ...base, keyCode, text: action === "down" ? key.slice(0, MAX_KEY_TEXT_CHARS) : "", modifiers };
}

/** The button a pointer event names, as the broker writes it. */
export function buttonOf(button: number): MouseButton {
  return (["left", "middle", "right", "back", "forward"] as const)[button] ?? "none";
}

/** A wheel's movement in CSS pixels: a line is 16, a page the height of the view. */
export function wheelDelta(delta: number, mode: number, pageHeight: number): number {
  const pixels = mode === 1 ? delta * 16 : mode === 2 ? delta * pageHeight : delta;
  return Math.round(clamp(pixels, -MAX_COORDINATE, MAX_COORDINATE) * 100) / 100;
}

/** A tab's name in the strip: its title, else its address without the scheme, else the blank page's. */
export function tabName(tab: BrowserTab): string {
  if (tab.title.trim()) return tab.title.trim();
  return tab.url.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").replace(/\/$/, "");
}
