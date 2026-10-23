import { describe, expect, it } from "vitest";

import {
  ackFrame,
  browserHello,
  browserUrl,
  buttonOf,
  fitted,
  FRAME_ACK,
  FRAME_INPUT,
  FRAME_LINK,
  FRAME_SCREEN,
  FRAME_STATE,
  inputFrame,
  keyEventOf,
  MAX_FRAME_BYTES,
  modifiersOf,
  openUrl,
  parseFrame,
  pasteChunks,
  tabName,
  toPage,
  wheelDelta,
} from "./browser-view-model";

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function frame(kind: number, body: unknown): ArrayBuffer {
  const payload = encoder.encode(JSON.stringify(body));
  const bytes = new Uint8Array(payload.length + 1);
  bytes[0] = kind;
  bytes.set(payload, 1);
  return bytes.buffer;
}

function screen(header: unknown, jpeg: number[]): ArrayBuffer {
  const encoded = encoder.encode(JSON.stringify(header));
  const bytes = new Uint8Array(1 + 4 + encoded.length + jpeg.length);
  bytes[0] = FRAME_SCREEN;
  new DataView(bytes.buffer).setUint32(1, encoded.length);
  bytes.set(encoded, 5);
  bytes.set(jpeg, 5 + encoded.length);
  return bytes.buffer;
}

const body = (bytes: Uint8Array) => JSON.parse(decoder.decode(bytes.subarray(1)));

describe("the socket", () => {
  it("opens on the page's origin, wss under https", () => {
    expect(browserUrl({ protocol: "https:", host: "hub.example" }, "northwind", 42)).toBe("wss://hub.example/v1/projects/northwind/runs/42/browser");
    expect(browserUrl({ protocol: "http:", host: "127.0.0.1:3000" }, "lumen labs", 1)).toBe("ws://127.0.0.1:3000/v1/projects/lumen%20labs/runs/1/browser");
  });

  it("says hello with the CSRF value as text", () => {
    expect(JSON.parse(browserHello("token-1"))).toEqual({ csrf: "token-1" });
  });

  it("frames input and acks as a type byte and a JSON body", () => {
    const input = inputFrame({ type: "text", text: "xin chào" });
    expect(input[0]).toBe(FRAME_INPUT);
    expect(body(input)).toEqual({ type: "text", text: "xin chào" });
    const ack = ackFrame(12);
    expect(ack[0]).toBe(FRAME_ACK);
    expect(body(ack)).toEqual({ seq: 12 });
  });
});

describe("parseFrame", () => {
  it("reads a screen frame's header and JPEG", () => {
    const parsed = parseFrame(screen({ seq: 3, width: 1280, height: 800, top: 0, scale: 1 }, [0xff, 0xd8, 0xff]));
    expect(parsed?.type).toBe("screen");
    if (parsed?.type !== "screen") return;
    expect(parsed.header).toEqual({ seq: 3, width: 1280, height: 800, top: 0, scale: 1 });
    expect([...parsed.jpeg]).toEqual([0xff, 0xd8, 0xff]);
  });

  it("refuses a screen frame whose header is cut or says no size", () => {
    const cut = new Uint8Array(screen({ seq: 1, width: 10, height: 10 }, [])).slice(0, 8);
    expect(parseFrame(cut.buffer)).toBeNull();
    expect(parseFrame(screen({ seq: 1, width: 0, height: 10 }, [1]))).toBeNull();
    expect(parseFrame(screen({ seq: "1", width: 10, height: 10 }, [1]))).toBeNull();
  });

  it("reads the state, its tabs and a notice", () => {
    const state = {
      mode: "human",
      tabs: [
        { id: "A1", url: "https://northwind.example/orders", title: "Orders" },
        { id: "B2", url: "https://lumen.example/", title: "" },
        { url: "no id" },
      ],
      active: "B2",
      notice: "the URL box opens http and https addresses only",
    };
    expect(parseFrame(frame(FRAME_STATE, state))).toEqual({
      type: "state",
      state: {
        mode: "human",
        tabs: [
          { id: "A1", url: "https://northwind.example/orders", title: "Orders" },
          { id: "B2", url: "https://lumen.example/", title: "" },
        ],
        active: "B2",
        notice: "the URL box opens http and https addresses only",
      },
    });
  });

  it("watches the first tab when the state names none it lists, and refuses a mode it does not know", () => {
    const parsed = parseFrame(frame(FRAME_STATE, { mode: "agent", tabs: [{ id: "A1", url: "about:blank", title: "" }], active: "Z9" }));
    expect(parsed).toEqual({ type: "state", state: { mode: "agent", tabs: [{ id: "A1", url: "about:blank", title: "" }], active: "A1", notice: null } });
    expect(parseFrame(frame(FRAME_STATE, { mode: "robot", tabs: [] }))).toBeNull();
  });

  it("reads the link and ignores text, unknown types and frames too large", () => {
    expect(parseFrame(frame(FRAME_LINK, { worker: true }))).toEqual({ type: "link", worker: true });
    expect(parseFrame(frame(FRAME_LINK, { worker: "yes" }))).toBeNull();
    expect(parseFrame("hello")).toBeNull();
    expect(parseFrame(frame(4, { viewing: true, login: false }))).toBeNull();
    expect(parseFrame(new ArrayBuffer(MAX_FRAME_BYTES + 1))).toBeNull();
    expect(parseFrame(new ArrayBuffer(0))).toBeNull();
  });
});

describe("pasteChunks", () => {
  it("keeps short text whole and splits long text without breaking a character", () => {
    expect(pasteChunks("chào")).toEqual(["chào"]);
    expect(pasteChunks("")).toEqual([]);
    expect(pasteChunks("abcdef", 4)).toEqual(["abcd", "ef"]);
    const emoji = "ab\u{1F600}cd";
    const chunks = pasteChunks(emoji, 3);
    expect(chunks.join("")).toBe(emoji);
    expect(chunks[0]).toBe("ab");
    expect(chunks[1]).toBe("\u{1F600}c");
  });

  it("keeps every chunk of the largest paste inside a frame", () => {
    const worst = "\u0001".repeat(130_000);
    for (const chunk of pasteChunks(worst)) expect(inputFrame({ type: "paste", text: chunk }).length).toBeLessThanOrEqual(MAX_FRAME_BYTES);
  });
});

describe("pointer", () => {
  const box = { left: 100, top: 50, width: 800, height: 600 };

  it("fits the picture in its box, centered", () => {
    expect(fitted(box, 1280, 800)).toEqual({ left: 100, top: 100, width: 800, height: 500 });
    expect(fitted(box, 0, 800).width).toBe(0);
  });

  it("maps a point of the picture to the page's CSS pixels", () => {
    const shown = fitted(box, 1280, 800);
    expect(toPage(100, 100, shown, { width: 1280, height: 800 })).toEqual({ x: 0, y: 0 });
    expect(toPage(500, 350, shown, { width: 1280, height: 800 })).toEqual({ x: 640, y: 400 });
    expect(toPage(900, 600, shown, { width: 1280, height: 800 })).toEqual({ x: 1280, y: 800 });
  });

  it("ignores a point beside the picture unless a drag keeps it at the edge", () => {
    const shown = fitted(box, 1280, 800);
    expect(toPage(500, 70, shown, { width: 1280, height: 800 })).toBeNull();
    expect(toPage(500, 70, shown, { width: 1280, height: 800 }, true)).toEqual({ x: 640, y: 0 });
  });

  it("names the buttons and measures the wheel in pixels", () => {
    expect([0, 1, 2, 3, 4, 9].map(buttonOf)).toEqual(["left", "middle", "right", "back", "forward", "none"]);
    expect(wheelDelta(3, 1, 800)).toBe(48);
    expect(wheelDelta(1, 2, 800)).toBe(800);
    expect(wheelDelta(-12.3456, 0, 800)).toBe(-12.35);
  });
});

const key = (key: string, more: Partial<KeyboardEvent> = {}) => ({
  key,
  code: more.code ?? "",
  keyCode: more.keyCode ?? 0,
  isComposing: more.isComposing ?? false,
  altKey: more.altKey ?? false,
  ctrlKey: more.ctrlKey ?? false,
  metaKey: more.metaKey ?? false,
  shiftKey: more.shiftKey ?? false,
});

describe("keys", () => {
  it("sends Command as Ctrl on an Apple device and as Meta elsewhere", () => {
    expect(modifiersOf(key("a", { metaKey: true }), true)).toBe(2);
    expect(modifiersOf(key("a", { metaKey: true }), false)).toBe(4);
    expect(modifiersOf(key("a", { altKey: true, shiftKey: true }), false)).toBe(9);
  });

  it("types a printable key as text and a named key as a key", () => {
    expect(keyEventOf(key("a", { code: "KeyA", keyCode: 65 }), "down", false)).toEqual({ type: "key", action: "down", key: "a", code: "KeyA", keyCode: 65, text: "a", modifiers: 0 });
    expect(keyEventOf(key("A", { code: "KeyA", shiftKey: true }), "down", false)).toMatchObject({ keyCode: 65, text: "A", modifiers: 8 });
    expect(keyEventOf(key(" ", { code: "Space" }), "down", false)).toMatchObject({ keyCode: 32, text: " " });
    expect(keyEventOf(key("Enter", { code: "Enter" }), "down", false)).toMatchObject({ keyCode: 13, text: "\r" });
    expect(keyEventOf(key("Enter", { code: "Enter" }), "up", false)).toMatchObject({ keyCode: 13, text: "" });
    expect(keyEventOf(key("Backspace"), "down", false)).toMatchObject({ keyCode: 8, text: "" });
    expect(keyEventOf(key("ArrowLeft"), "down", false)).toMatchObject({ keyCode: 37, text: "" });
    expect(keyEventOf(key("F5"), "down", false)).toMatchObject({ keyCode: 116, text: "" });
    expect(keyEventOf(key("a", { code: "KeyA" }), "up", false)).toMatchObject({ action: "up", text: "" });
  });

  it("sends a shortcut as a key without text", () => {
    expect(keyEventOf(key("a", { code: "KeyA", ctrlKey: true }), "down", false)).toMatchObject({ keyCode: 65, text: "", modifiers: 2 });
    expect(keyEventOf(key("a", { code: "KeyA", metaKey: true }), "down", true)).toMatchObject({ keyCode: 65, text: "", modifiers: 2 });
    expect(keyEventOf(key("f", { code: "KeyF", altKey: true }), "down", false)).toMatchObject({ text: "", modifiers: 1 });
  });

  it("types what Option and AltGr make as text", () => {
    expect(keyEventOf(key("å", { code: "KeyA", altKey: true }), "down", true)).toMatchObject({ keyCode: 65, text: "å", modifiers: 0 });
    expect(keyEventOf(key("@", { code: "KeyQ", ctrlKey: true, altKey: true }), "down", false)).toMatchObject({ text: "@", modifiers: 0 });
  });

  it("leaves composition, dead keys and the paste shortcut to the text box", () => {
    expect(keyEventOf(key("a", { isComposing: true }), "down", false)).toBeNull();
    expect(keyEventOf(key("Process", { keyCode: 229 }), "down", false)).toBeNull();
    expect(keyEventOf(key("Unidentified", { keyCode: 229 }), "down", false)).toBeNull();
    expect(keyEventOf(key("Dead", { code: "Quote" }), "down", false)).toBeNull();
    expect(keyEventOf(key("v", { code: "KeyV", ctrlKey: true }), "down", false)).toBeNull();
    expect(keyEventOf(key("v", { code: "KeyV", metaKey: true }), "down", true)).toBeNull();
    expect(keyEventOf(key("v", { code: "KeyV", ctrlKey: true }), "up", false)).toMatchObject({ action: "up" });
  });
});

describe("the URL box and the tabs", () => {
  it("opens what was typed, trimmed", () => {
    expect(openUrl("  northwind.example/orders ")).toBe("northwind.example/orders");
    expect(openUrl("   ")).toBeNull();
    expect(openUrl("x".repeat(8193))).toBeNull();
  });

  it("names a tab by its title, else its address", () => {
    expect(tabName({ id: "A1", url: "https://lumen.example/", title: " Lumen " })).toBe("Lumen");
    expect(tabName({ id: "A1", url: "https://lumen.example/", title: "" })).toBe("lumen.example");
    expect(tabName({ id: "A1", url: "about:blank", title: "" })).toBe("about:blank");
  });
});
