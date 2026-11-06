/**
 * The bridge evo-answer/1 between an interactive answer and the chat around it (docs/hub.md, Interactive answers):
 * the page an agent wrote runs in a frame of an opaque origin (`interactive-answer.tsx`), posts requests to the web,
 * and the web answers each one. It has four methods and no other: `context` (the viewer's theme and language),
 * `resize` (the height the page asks, kept within ANSWER_HEIGHT), `sendPrompt({ text })`, which sends the viewer's
 * next question to the agent that wrote the answer, and `openLink({ url })`, which opens an https URL in a new tab.
 * This module is the protocol without the DOM: what a message must be and what each method asks of the host.
 *
 * A request is `{bridge: "evo-answer/1", id, method, params}`; the answer `{bridge, id, result}` or
 * `{bridge, id, error: {code, message, status?}}`. The web also posts `{bridge, event: "theme", theme}` when the
 * viewer's theme changes. The hub's helper /v1/plugin-ui/kit/1/evo-answer.js speaks it for a page.
 */

export const ANSWER_BRIDGE = "evo-answer/1";
/** The frame's height: what it starts at, and the bounds a page's `resize` is kept within. */
export const ANSWER_HEIGHT = { min: 120, max: 1600, initial: 320 };
export const MAX_PROMPT_CHARS = 4000; // chats.MAX_ANSWER_PROMPT_CHARS of the hub
/** At most PROMPTS_PER_WINDOW questions from one frame in WINDOW_MS, as the hub holds per person (chats.ANSWER_PROMPTS). */
export const PROMPTS_PER_WINDOW = 6;
export const PROMPT_WINDOW_MS = 60_000;
export const MAX_URL_CHARS = 2048;
const MAX_ID_CHARS = 100;

export type AnswerErrorCode =
  | "unsupported_bridge"
  | "invalid_request"
  | "unknown_method"
  | "invalid_params"
  | "busy"
  | "rate_limited"
  | "hub_refused"
  | "hub_unavailable";

export class AnswerError extends Error {
  constructor(
    readonly code: AnswerErrorCode,
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "AnswerError";
  }
}

export type AnswerTheme = "light" | "dark";
export type AnswerContext = { bridge: typeof ANSWER_BRIDGE; theme: AnswerTheme; locale: string };
export type AnswerRequest = { id: string | number; method: string; params: Record<string, unknown> };

export const ANSWER_METHODS = ["context", "resize", "sendPrompt", "openLink"] as const;
export type AnswerMethod = (typeof ANSWER_METHODS)[number];

export type AnswerHost = {
  context: () => AnswerContext;
  /** Sets the frame's height, already within ANSWER_HEIGHT. */
  resize: (height: number) => void;
  /** Sends `text` (trimmed, 1 to MAX_PROMPT_CHARS characters) as the viewer's message from this answer; its id. */
  sendPrompt: (text: string) => Promise<{ message_id: number }>;
  /** Opens `url`, an https URL, in a new tab with noopener and noreferrer. */
  openLink: (url: string) => void;
  /** The questions this frame may send: one at a time, PROMPTS_PER_WINDOW in PROMPT_WINDOW_MS. */
  limiter: PromptLimiter;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The request a message holds; null for a message that is not one of the bridge's (it is ignored), an AnswerError
 * for one that is but cannot be answered as asked, with the id to answer it under when it has one.
 */
export function readAnswerRequest(data: unknown): AnswerRequest | null | { id: string | number | null; error: AnswerError } {
  if (!isRecord(data) || typeof data.bridge !== "string" || !data.bridge.startsWith("evo-answer/")) return null;
  const id = typeof data.id === "string" || typeof data.id === "number" ? data.id : null;
  if (data.bridge !== ANSWER_BRIDGE) {
    return { id, error: new AnswerError("unsupported_bridge", `this hub speaks ${ANSWER_BRIDGE}, not ${data.bridge}`) };
  }
  if (id === null || (typeof id === "string" && id.length > MAX_ID_CHARS)) {
    return { id: null, error: new AnswerError("invalid_request", "a request needs an id, a string or a number") };
  }
  if (typeof data.method !== "string") return { id, error: new AnswerError("invalid_request", "a request names its method") };
  const params = data.params ?? {};
  if (!isRecord(params)) return { id, error: new AnswerError("invalid_params", "params is an object") };
  return { id, method: data.method, params };
}

/** The text of a question, trimmed, when it is 1 to MAX_PROMPT_CHARS characters; an AnswerError otherwise. */
export function promptText(value: unknown): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text || [...text].length > MAX_PROMPT_CHARS) {
    throw new AnswerError("invalid_params", `text is the question, 1 to ${MAX_PROMPT_CHARS} characters after trimming`);
  }
  return text;
}

/** `value` as an https URL to open, normalised; an AnswerError for any other scheme, credentials or a bad URL. */
export function linkTarget(value: unknown): string {
  const refused = new AnswerError("invalid_params", "url is an https URL, such as https://example.com/");
  if (typeof value !== "string" || value.length > MAX_URL_CHARS || /[\u0000-\u001f\u007f\s]/.test(value)) throw refused;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw refused;
  }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password) throw refused;
  return url.href;
}

/**
 * How many questions one frame may send: one at a time, and at most PROMPTS_PER_WINDOW in any PROMPT_WINDOW_MS.
 * `now` is the clock, a test's own.
 */
export class PromptLimiter {
  private sent: number[] = [];
  private busy = false;

  constructor(
    private readonly limit = PROMPTS_PER_WINDOW,
    private readonly windowMs = PROMPT_WINDOW_MS,
    private readonly now: () => number = Date.now,
  ) {}

  /** Takes a place for a question, or throws `busy` or `rate_limited`; `release` gives it back when done. */
  take(): void {
    if (this.busy) throw new AnswerError("busy", "a question from this answer is being sent");
    const now = this.now();
    this.sent = this.sent.filter((at) => now - at < this.windowMs);
    if (this.sent.length >= this.limit) {
      throw new AnswerError("rate_limited", `at most ${this.limit} questions from an answer in ${this.windowMs / 1000} seconds`, 429);
    }
    this.sent.push(now);
    this.busy = true;
  }

  release(): void {
    this.busy = false;
  }
}

/** A height a page asked, as a whole number of pixels within ANSWER_HEIGHT; an AnswerError for anything else. */
export function answerHeight(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new AnswerError("invalid_params", "height is a number of pixels");
  }
  return Math.min(ANSWER_HEIGHT.max, Math.max(ANSWER_HEIGHT.min, Math.ceil(value)));
}

function isMethod(name: string): name is AnswerMethod {
  return (ANSWER_METHODS as readonly string[]).includes(name);
}

/** The answer to `request` from the frame `host` holds; rejects with an AnswerError. */
export async function answerRequest(request: AnswerRequest, host: AnswerHost): Promise<unknown> {
  const { method, params } = request;
  if (!isMethod(method)) throw new AnswerError("unknown_method", `${ANSWER_BRIDGE} has no method ${method}`);
  switch (method) {
    case "context":
      return host.context();
    case "resize": {
      const height = answerHeight(params.height);
      host.resize(height);
      return { height };
    }
    case "openLink": {
      const url = linkTarget(params.url);
      host.openLink(url);
      return { opened: true };
    }
    case "sendPrompt": {
      const text = promptText(params.text);
      host.limiter.take();
      try {
        return await host.sendPrompt(text);
      } finally {
        host.limiter.release();
      }
    }
  }
}

/** The message that answers request `id`: its result, or the error it failed with. */
export function answerReply(id: string | number | null, outcome: { result: unknown } | { error: unknown }) {
  if ("result" in outcome) return { bridge: ANSWER_BRIDGE, id, result: outcome.result ?? null };
  const error = outcome.error;
  if (error instanceof AnswerError) {
    return { bridge: ANSWER_BRIDGE, id, error: { code: error.code, message: error.message, status: error.status } };
  }
  return { bridge: ANSWER_BRIDGE, id, error: { code: "hub_unavailable", message: "the hub did not answer" } };
}
