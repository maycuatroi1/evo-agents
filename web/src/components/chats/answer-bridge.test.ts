import { describe, expect, it, vi } from "vitest";

import {
  ANSWER_BRIDGE,
  ANSWER_HEIGHT,
  AnswerError,
  type AnswerHost,
  answerReply,
  answerRequest,
  linkTarget,
  MAX_PROMPT_CHARS,
  PROMPT_WINDOW_MS,
  PromptLimiter,
  PROMPTS_PER_WINDOW,
  promptText,
  readAnswerRequest,
} from "./answer-bridge";

type TestHost = AnswerHost & { sent: string[]; opened: string[]; heights: number[]; clock: { now: number } };

function host(send?: (text: string) => Promise<{ message_id: number }>): TestHost {
  const sent: string[] = [];
  const opened: string[] = [];
  const heights: number[] = [];
  const clock = { now: 1_000_000 };
  return {
    sent,
    opened,
    heights,
    clock,
    context: () => ({ bridge: ANSWER_BRIDGE, theme: "dark", locale: "vi" }),
    resize: (height) => heights.push(height),
    openLink: (url) => opened.push(url),
    sendPrompt:
      send ??
      (async (text) => {
        sent.push(text);
        return { message_id: 40 + sent.length };
      }),
    limiter: new PromptLimiter(PROMPTS_PER_WINDOW, PROMPT_WINDOW_MS, () => clock.now),
  };
}

const ask = (method: string, params: Record<string, unknown> = {}) => ({ id: 1, method, params });

async function refusal(promise: Promise<unknown>): Promise<AnswerError> {
  const error = await promise.then(
    () => null,
    (cause: unknown) => cause,
  );
  expect(error).toBeInstanceOf(AnswerError);
  return error as AnswerError;
}

describe("reading a message of the bridge evo-answer/1", () => {
  it("ignores what is not the bridge's, and refuses another version, a missing id or method, and params not an object", () => {
    expect(readAnswerRequest("hello")).toBeNull();
    expect(readAnswerRequest({ bridge: "evo-plugin/1", id: 1, method: "context" })).toBeNull();
    expect(readAnswerRequest({ bridge: "evo-answer/2", id: 3, method: "context" })).toMatchObject({ id: 3, error: { code: "unsupported_bridge" } });
    expect(readAnswerRequest({ bridge: ANSWER_BRIDGE, method: "context" })).toMatchObject({ id: null, error: { code: "invalid_request" } });
    expect(readAnswerRequest({ bridge: ANSWER_BRIDGE, id: "x".repeat(101), method: "context" })).toMatchObject({ error: { code: "invalid_request" } });
    expect(readAnswerRequest({ bridge: ANSWER_BRIDGE, id: 2 })).toMatchObject({ id: 2, error: { code: "invalid_request" } });
    expect(readAnswerRequest({ bridge: ANSWER_BRIDGE, id: 2, method: "resize", params: [1] })).toMatchObject({ error: { code: "invalid_params" } });
    expect(readAnswerRequest({ bridge: ANSWER_BRIDGE, id: "a", method: "context" })).toEqual({ id: "a", method: "context", params: {} });
  });
});

describe("the methods of evo-answer/1", () => {
  it("gives the context: the bridge, the theme and the language, and nothing of the viewer or the hub", async () => {
    expect(await answerRequest(ask("context"), host())).toEqual({ bridge: ANSWER_BRIDGE, theme: "dark", locale: "vi" });
  });

  it("refuses any method but its four", async () => {
    for (const method of ["navigate", "toast", "route", "runs.list", "__proto__", "toString", "sendprompt"]) {
      expect((await refusal(answerRequest(ask(method), host()))).code).toBe("unknown_method");
    }
  });

  it("keeps a resize within its bounds and refuses a height that is not a number", async () => {
    const frame = host();
    expect(await answerRequest(ask("resize", { height: 480.2 }), frame)).toEqual({ height: 481 });
    await answerRequest(ask("resize", { height: 10 }), frame);
    await answerRequest(ask("resize", { height: 1e9 }), frame);
    expect(frame.heights).toEqual([481, ANSWER_HEIGHT.min, ANSWER_HEIGHT.max]);
    for (const height of ["480", -1, Number.NaN, Number.POSITIVE_INFINITY, undefined]) {
      expect((await refusal(answerRequest(ask("resize", { height }), frame))).code).toBe("invalid_params");
    }
  });

  it("sends a question of 1 to 4000 characters after trimming, and refuses an empty or longer one", async () => {
    const frame = host();
    expect(await answerRequest(ask("sendPrompt", { text: "  Compare Q3 too  " }), frame)).toEqual({ message_id: 41 });
    await answerRequest(ask("sendPrompt", { text: "ế".repeat(MAX_PROMPT_CHARS) }), frame);
    expect(frame.sent).toEqual(["Compare Q3 too", "ế".repeat(MAX_PROMPT_CHARS)]);
    for (const text of ["", "   \n", "x".repeat(MAX_PROMPT_CHARS + 1), 12, undefined]) {
      expect((await refusal(answerRequest(ask("sendPrompt", { text }), frame))).code).toBe("invalid_params");
    }
    expect(frame.sent).toHaveLength(2);
  });

  it("sends at most 6 questions from a frame in 60 seconds, then more once the window moved on", async () => {
    const frame = host();
    for (let index = 0; index < PROMPTS_PER_WINDOW; index += 1) {
      await answerRequest(ask("sendPrompt", { text: `q${index}` }), frame);
      frame.clock.now += 1_000;
    }
    const limited = await refusal(answerRequest(ask("sendPrompt", { text: "one more" }), frame));
    expect([limited.code, limited.status]).toEqual(["rate_limited", 429]);
    expect(frame.sent).toHaveLength(PROMPTS_PER_WINDOW);
    frame.clock.now += PROMPT_WINDOW_MS - PROMPTS_PER_WINDOW * 1_000 + 1;
    await answerRequest(ask("sendPrompt", { text: "later" }), frame);
    expect(frame.sent.at(-1)).toBe("later");
  });

  it("sends one question at a time from a frame, and frees the place when the hub refuses one", async () => {
    let release: (value: { message_id: number }) => void = () => {};
    const send = vi.fn(() => new Promise<{ message_id: number }>((resolve) => (release = resolve)));
    const frame = host(send);
    const first = answerRequest(ask("sendPrompt", { text: "first" }), frame);
    expect((await refusal(answerRequest(ask("sendPrompt", { text: "second" }), frame))).code).toBe("busy");
    release({ message_id: 7 });
    expect(await first).toEqual({ message_id: 7 });

    const failing = host(async () => {
      throw new AnswerError("hub_refused", "no such answer", 404);
    });
    expect((await refusal(answerRequest(ask("sendPrompt", { text: "a" }), failing))).code).toBe("hub_refused");
    expect((await refusal(answerRequest(ask("sendPrompt", { text: "b" }), failing))).code).toBe("hub_refused");
  });

  it("opens only an https URL, without credentials, and refuses any other", async () => {
    const frame = host();
    expect(await answerRequest(ask("openLink", { url: "https://example.com/report?q=1#top" }), frame)).toEqual({ opened: true });
    expect(frame.opened).toEqual(["https://example.com/report?q=1#top"]);
    const refused = [
      "http://example.com/",
      "javascript:alert(1)",
      "data:text/html,hi",
      "//example.com/",
      "/chats/1",
      "https://user:pass@example.com/",
      "https://exa mple.com/",
      `https://example.com/${"a".repeat(2048)}`,
      42,
    ];
    for (const url of refused) {
      expect((await refusal(answerRequest(ask("openLink", { url }), frame))).code, String(url)).toBe("invalid_params");
    }
    expect(frame.opened).toHaveLength(1);
  });
});

describe("the helpers of evo-answer/1", () => {
  it("trims a question and normalises a link", () => {
    expect(promptText("\n hi \t")).toBe("hi");
    expect(linkTarget("https://EXAMPLE.com")).toBe("https://example.com/");
    expect(() => linkTarget("ftp://example.com/")).toThrow(AnswerError);
  });

  it("answers with the result, or with the error's code, message and status, and hides any other failure", () => {
    expect(answerReply(3, { result: { height: 200 } })).toEqual({ bridge: ANSWER_BRIDGE, id: 3, result: { height: 200 } });
    expect(answerReply(3, { result: undefined })).toEqual({ bridge: ANSWER_BRIDGE, id: 3, result: null });
    expect(answerReply("a", { error: new AnswerError("rate_limited", "slow down", 429) })).toEqual({
      bridge: ANSWER_BRIDGE,
      id: "a",
      error: { code: "rate_limited", message: "slow down", status: 429 },
    });
    expect(answerReply(4, { error: new Error("secret stack") })).toEqual({
      bridge: ANSWER_BRIDGE,
      id: 4,
      error: { code: "hub_unavailable", message: "the hub did not answer" },
    });
  });
});
