// @vitest-environment jsdom
import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";

import { useRunTerminal } from "./use-run-terminal";

const csrf = vi.hoisted(() => ({ status: 200 }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async () =>
        csrf.status === 200
          ? Response.json({ csrf: "csrf-value", header: "X-Evo-CSRF" })
          : Response.json({ detail: "sign in" }, { status: csrf.status }),
    }),
}));

/** A WebSocket the test drives: open it, deliver frames, close it as the hub would. */
class FakeSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static made: FakeSocket[] = [];
  readyState = 0;
  binaryType = "blob";
  sent: (string | Uint8Array)[] = [];
  closedWith: [number | undefined, string | undefined] | null = null;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(readonly url: string) {
    FakeSocket.made.push(this);
  }
  send(data: string | Uint8Array) {
    this.sent.push(typeof data === "string" ? data : new Uint8Array(data));
  }
  close(code?: number, reason?: string) {
    this.closedWith = [code, reason];
    this.readyState = 2;
  }
  open() {
    this.readyState = 1;
    this.onopen?.(new Event("open"));
  }
  output(text: string) {
    const bytes = new TextEncoder().encode(text);
    const frame = new Uint8Array(bytes.length + 1);
    frame[0] = 1;
    frame.set(bytes, 1);
    this.onmessage?.(new MessageEvent("message", { data: frame.buffer }));
  }
  hubCloses(code: number, reason = "") {
    this.readyState = 3;
    this.onclose?.(new CloseEvent("close", { code, reason }));
  }
}

function setup(sessionOld = false) {
  const output: string[] = [];
  const hook = renderHook(() =>
    useRunTerminal({
      project: "demo",
      runId: 12,
      onOutput: (bytes) => output.push(new TextDecoder().decode(bytes)),
      sessionOld: () => sessionOld,
    }),
  );
  return { hook, output };
}

async function connected(sessionOld = false) {
  const made = setup(sessionOld);
  act(() => made.hook.result.current.connect(100, 30));
  await waitFor(() => expect(FakeSocket.made).toHaveLength(1));
  const socket = FakeSocket.made[0];
  return { ...made, socket };
}

beforeEach(() => {
  FakeSocket.made = [];
  csrf.status = 200;
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useRunTerminal", () => {
  it("opens the socket on the page's origin and says hello with the CSRF value and the size", async () => {
    const { hook, socket } = await connected();
    expect(socket.url).toBe(`ws://${window.location.host}/v1/projects/demo/runs/12/terminal`);
    expect(socket.binaryType).toBe("arraybuffer");
    expect(hook.result.current.status).toBe("connecting");
    act(() => socket.open());
    expect(JSON.parse(socket.sent[0] as string)).toEqual({ csrf: "csrf-value", cols: 100, rows: 30 });
    expect(hook.result.current.status).toBe("waiting");
    expect(hook.result.current.size).toEqual({ cols: 100, rows: 30 });
  });

  it("sends no input until the worker's end answers, then sends each key as an input frame", async () => {
    const { hook, socket, output } = await connected();
    act(() => socket.open());
    expect(hook.result.current.send("early")).toBe(false);
    expect(socket.sent).toHaveLength(1);

    act(() => socket.output("$ "));
    expect(hook.result.current.status).toBe("live");
    expect(output).toEqual(["$ "]);
    expect(hook.result.current.send("ls\r")).toBe(true);
    expect([...(socket.sent[1] as Uint8Array)]).toEqual([0, 108, 115, 13]);
  });

  it("sends a resize frame once the socket is open, and only when the size changes", async () => {
    const { hook, socket } = await connected();
    act(() => hook.result.current.resize(90, 20)); // before the socket opens: the hello carries it
    act(() => socket.open());
    expect(JSON.parse(socket.sent[0] as string)).toMatchObject({ cols: 90, rows: 20 });
    act(() => hook.result.current.resize(90, 20));
    expect(socket.sent).toHaveLength(1);
    act(() => hook.result.current.resize(120, 40));
    expect([...(socket.sent[1] as Uint8Array)]).toEqual([2, 0, 120, 0, 40]);
    expect(hook.result.current.size).toEqual({ cols: 120, rows: 40 });
  });

  it("tells each close of the hub apart", async () => {
    for (const [code, kind] of [
      [4409, "busy"],
      [4403, "forbidden"],
      [4401, "signIn"],
      [1000, "ended"],
      [1006, "lost"],
    ] as const) {
      FakeSocket.made = [];
      const { hook, socket } = await connected();
      act(() => socket.open());
      act(() => socket.hubCloses(code, "the hub's reason"));
      expect(hook.result.current.status).toBe("closed");
      expect(hook.result.current.end).toEqual({ code, reason: "the hub's reason", kind });
      hook.unmount();
    }
  });

  it("reads a 4403 as sign in again when the sign-in is older than 12 hours", async () => {
    const { hook, socket } = await connected(true);
    act(() => socket.open());
    act(() => socket.hubCloses(4403, "the web session is older than 12 hours: sign in again"));
    expect(hook.result.current.end?.kind).toBe("signIn");
  });

  it("asks to sign in again when the CSRF value cannot be read for want of a session", async () => {
    csrf.status = 401;
    const { hook } = setup();
    act(() => hook.result.current.connect(80, 24));
    await waitFor(() => expect(hook.result.current.status).toBe("closed"));
    expect(hook.result.current.end?.kind).toBe("signIn");
    expect(FakeSocket.made).toHaveLength(0);
  });

  it("Disconnect closes the socket with 1000 and reads as the person's own close", async () => {
    const { hook, socket } = await connected();
    act(() => socket.open());
    act(() => hook.result.current.disconnect());
    expect(socket.closedWith).toEqual([1000, "closed in the browser"]);
    act(() => socket.hubCloses(1000));
    expect(hook.result.current.end?.kind).toBe("closed");
  });

  it("closes the socket when the page goes", async () => {
    const { hook, socket } = await connected();
    act(() => socket.open());
    hook.unmount();
    expect(socket.closedWith?.[0]).toBe(1000);
  });
});
