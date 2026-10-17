import { describe, expect, it, vi } from "vitest";

import {
  allowedMethods,
  answer,
  BRIDGE,
  type BridgeContext,
  BridgeError,
  type BridgeRequest,
  FRAME_HEIGHT,
  type Host,
  MAX_PARAMS_BYTES,
  readRequest,
  reply,
} from "./bridge";

const CONTEXT: BridgeContext = {
  bridge: BRIDGE,
  project: "demo",
  plugin: "sample",
  page: "board",
  viewer: { login: "octo", role: "reader" },
  locale: "en",
  theme: "light",
  methods: [],
};

type TestHost = Host & { calls: string[]; grant: string[]; reads: number };

/** A host whose hub records each call, granting the plugin `grant`, which a test changes as a project would. */
function host(): TestHost {
  const calls: string[] = [];
  const record =
    (name: string, result: unknown = { ok: name }) =>
    async (...args: unknown[]) => {
      calls.push(`${name} ${JSON.stringify(args)}`);
      return result;
    };
  const page: TestHost = {
    calls,
    grant: [],
    reads: 0,
    context: () => CONTEXT,
    resize: vi.fn(),
    granted: async () => {
      page.reads += 1;
      return page.grant;
    },
    hub: {
      runs: record("runs", { runs: [], total: 0 }),
      run: record("run"),
      plans: record("plans", []),
      plan: record("plan"),
      memories: record("memories"),
      route: record("route", { status: 200, body: { hello: "world" } }) as Host["hub"]["route"],
    },
  };
  return page;
}

/** `request` answered for a page whose plugin the project grants `grant` now. */
function ask(page: TestHost, request: BridgeRequest, grant: string[]): Promise<unknown> {
  page.grant = grant;
  return answer(request, page);
}

const request = (method: string, params: Record<string, unknown> = {}): BridgeRequest => ({ id: 1, method, params });

async function refusal(promise: Promise<unknown>): Promise<BridgeError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof BridgeError) return error;
    throw error;
  }
  throw new Error("the call was not refused");
}

describe("bridge evo-plugin/1", () => {
  it("opens a method only when the project granted the permission it needs", () => {
    expect(allowedMethods([])).toEqual(["context", "resize", "route"]);
    expect(allowedMethods(["runs:read", "chats:write"])).toEqual(["context", "resize", "route", "runs.list", "runs.get"]);
    expect(allowedMethods(["plans:read", "memories:read"])).toContain("memories.list");
  });

  it("reads a request, ignores what is not the bridge's, and refuses what it cannot answer", () => {
    expect(readRequest("hello")).toBeNull();
    expect(readRequest({ type: "webpackOk" })).toBeNull();
    expect(readRequest({ bridge: BRIDGE, id: "a", method: "context" })).toEqual({ id: "a", method: "context", params: {} });
    const older = readRequest({ bridge: "evo-plugin/0", id: 3, method: "context" });
    expect(older).toMatchObject({ id: 3, error: { code: "unsupported_bridge" } });
    expect(readRequest({ bridge: BRIDGE, method: "context" })).toMatchObject({ id: null, error: { code: "invalid_request" } });
    expect(readRequest({ bridge: BRIDGE, id: 1, method: "x", params: [1] })).toMatchObject({ error: { code: "invalid_params" } });
    const big = { bridge: BRIDGE, id: 1, method: "route", params: { body: "x".repeat(MAX_PARAMS_BYTES) } };
    expect(readRequest(big)).toMatchObject({ error: { code: "too_large" } });
  });

  it("refuses a method the project did not grant before it reaches the hub", async () => {
    const page = host();
    const error = await refusal(ask(page, request("plans.list"), ["runs:read"]));
    expect(error.code).toBe("not_granted");
    expect(error.message).toBe("plans.list needs plans:read, which project demo did not grant this plugin");
    expect((await refusal(ask(page, request("secrets.list"), ["runs:read"]))).code).toBe("unknown_method");
    expect((await refusal(ask(page, request("toString"), []))).code).toBe("unknown_method");
    expect(page.calls).toEqual([]);
  });

  it("reads the grant again before each method that needs one, so a grant taken back closes it at once", async () => {
    const page = host();
    expect(await ask(page, request("runs.list"), ["runs:read"])).toEqual({ runs: [], total: 0 });
    expect(page.reads).toBe(1);
    // the project disabled the plugin, or an upgrade asked for less, while the page stayed open
    const error = await refusal(ask(page, request("runs.get", { id: 3 }), []));
    expect(error.code).toBe("not_granted");
    expect(page.reads).toBe(2);
    expect(page.calls).toEqual(['runs [{"limit":20,"offset":0}]']); // the refused call never reached the hub
    // what every page has asks for no grant
    await ask(page, request("context"), []);
    await ask(page, request("resize", { height: 300 }), []);
    await ask(page, request("route", { path: "hello" }), []);
    expect(page.reads).toBe(2);
  });

  it("answers a granted method through the hub with its checked params", async () => {
    const page = host();
    expect(await ask(page, request("context"), [])).toBe(CONTEXT);
    expect(await ask(page, request("runs.list", { state: ["running"], limit: 5 }), ["runs:read"])).toEqual({ runs: [], total: 0 });
    await ask(page, request("runs.get", { id: 12 }), ["runs:read"]);
    await ask(page, request("plans.get", { id: "hub-agent-plugins" }), ["plans:read"]);
    expect(page.calls).toEqual([
      'runs [{"state":["running"],"limit":5,"offset":0}]',
      "run [12]",
      'plan ["hub-agent-plugins"]',
    ]);
    const bad = [
      request("runs.list", { limit: 500 }),
      request("runs.list", { state: ["exploded"] }),
      request("runs.get", {}),
      request("plans.get", { id: "../etc" }),
    ];
    for (const each of bad) expect((await refusal(ask(page, each, ["runs:read", "plans:read"]))).code).toBe("invalid_params");
  });

  it("calls only the plugin's own routes, by a relative path", async () => {
    const page = host();
    const answered = await ask(page, request("route", { method: "POST", path: "items/12", body: { a: 1 } }), []);
    expect(answered).toEqual({ status: 200, body: { hello: "world" } });
    expect(page.calls).toEqual(['route [{"method":"POST","path":"items/12","query":{},"body":{"a":1}}]']);
    for (const path of ["/v1/plugins", "../../admin", "a/../b", "https://evil.test/x", "a b"]) {
      expect((await refusal(ask(page, request("route", { path }), []))).code).toBe("invalid_params");
    }
    expect((await refusal(ask(page, request("route", { path: "a", body: {} }), []))).code).toBe("invalid_params");
    expect((await refusal(ask(page, request("route", { path: "a", method: "TRACE" }), []))).code).toBe("invalid_params");
  });

  it("sizes the frame within its bounds", async () => {
    const page = host();
    expect(await ask(page, request("resize", { height: 99_999 }), [])).toEqual({ height: FRAME_HEIGHT.max });
    expect(page.resize).toHaveBeenCalledWith(FRAME_HEIGHT.max);
    expect(await ask(page, request("resize", { height: 10 }), [])).toEqual({ height: FRAME_HEIGHT.min });
    expect((await refusal(ask(page, request("resize", { height: "tall" }), []))).code).toBe("invalid_params");
  });

  it("replies with the result or the error, and never with what a failure held", () => {
    expect(reply(7, { result: undefined })).toEqual({ bridge: BRIDGE, id: 7, result: null });
    const refused = new BridgeError("hub_refused", "no access", 403);
    expect(reply(7, { error: refused })).toEqual({
      bridge: BRIDGE,
      id: 7,
      error: { code: "hub_refused", message: "no access", status: 403 },
    });
    expect(reply("x", { error: new Error("token evh_secret leaked") })).toEqual({
      bridge: BRIDGE,
      id: "x",
      error: { code: "hub_unavailable", message: "the hub did not answer" },
    });
  });
});
