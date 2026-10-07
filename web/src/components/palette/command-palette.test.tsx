// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { Overview, OverviewRun } from "@/components/home/model";
import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { CommandPaletteProvider, isPaletteShortcut } from "./palette-context";
import { PaletteTrigger } from "./palette-trigger";

const api = vi.hoisted(() => ({
  answers: {} as Record<string, unknown>,
  seen: [] as string[],
  /** The run searches the palette sent, with their abort signals. */
  searches: [] as { q: string | null; signal: AbortSignal }[],
  /** While set, a run search waits until this resolves (or its request is aborted). */
  gate: null as Promise<void> | null,
  /** Answers held back until their promise resolves, by path. */
  hold: {} as Record<string, Promise<void>>,
}));
const nav = vi.hoisted(() => ({ project: "demo" as string | undefined, push: vi.fn() }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        api.seen.push(`${request.method} ${url.pathname}${url.search}`);
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (/^\/v1\/projects\/[^/]+\/runs$/.test(url.pathname) && request.method === "GET") {
          const q = url.searchParams.get("q");
          api.searches.push({ q, signal: request.signal });
          if (api.gate) {
            const gate = api.gate;
            await new Promise<void>((resolve, reject) => {
              request.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
              void gate.then(resolve);
            });
          }
          const runs = (RUNS[url.pathname] ?? []).filter((item) => !q || `#${item.id} ${item.title}`.toLowerCase().includes(q.toLowerCase()));
          return Response.json({ runs, total: runs.length, counts: {}, limit: 8, offset: 0 });
        }
        if (url.pathname in api.hold) await api.hold[url.pathname];
        if (!(url.pathname in api.answers)) return Response.json({ error: "not_found", message: "no such thing" }, { status: 404 });
        return Response.json(api.answers[url.pathname]);
      },
    }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => (nav.project ? `/p/${nav.project}` : "/"),
  useParams: () => (nav.project ? { project: nav.project } : {}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

beforeAll(() => {
  // A keyboard that labels its modifier Ctrl, whatever machine runs the tests.
  Object.defineProperty(window.navigator, "platform", { value: "Linux x86_64", configurable: true });
  // cmdk measures its list and scrolls the selected item into view; jsdom has no layout.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Element.prototype.scrollIntoView ??= () => {};
  window.matchMedia ??= ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
});

function run(id: number, state: OverviewRun["state"], extra: Partial<OverviewRun> = {}): OverviewRun {
  return {
    id,
    kind: "step",
    project: "demo",
    plan_id: "rollout",
    plan_title: "Worker fleet rollout",
    step_key: "2",
    title: "Queue with dispatch and claim",
    state,
    dispatched_by: "octo",
    worker_id: 1,
    worker: "mini",
    runtime: "claude-code",
    model: null,
    steps_total: null,
    steps_done: null,
    run_seconds: 0,
    queued_at: "2026-10-07T01:00:00Z",
    started_at: "2026-10-07T01:01:00Z",
    finished_at: null,
    error: null,
    ...extra,
  };
}

/** What GET /v1/projects/{p}/runs holds, by path; the mock matches q against "#id title". */
const RUNS: Record<string, OverviewRun[]> = {
  "/v1/projects/demo/runs": [run(42, "review", { title: "Runs page on the web", step_key: "4" }), run(13, "running")],
};

type Grant = { project: string; role: string; max_level: string };

const PLAN = { revision: 1, digest: "d", updated_at: "2026-10-07T01:00:00Z", updated_by: "octo" };

function overview(grants: Grant[]): Overview {
  return {
    counts: { waiting_on_you: 0, running: 1, queued: 0, done_7d: 0, failed_7d: 1, lost_7d: 0 },
    done_by_day: [],
    active_runs: [run(13, "running")],
    recent_runs: [run(8, "failed", { step_key: "3", title: "Worker daemon", finished_at: "2026-10-07T00:50:00Z" })],
    open_decisions: [],
    projects: grants.map((grant) => ({ name: grant.project, role: grant.role, max_level: grant.max_level, repos: 1, active_plans: 1, open_decisions: 0 })),
  };
}

const WRITER: Grant[] = [
  { project: "demo", role: "writer", max_level: "internal" },
  { project: "docs", role: "reader", max_level: "public" },
];
const READER: Grant[] = [{ project: "demo", role: "reader", max_level: "public" }];

function setup(grants: Grant[] = WRITER) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const whoami = { login: "octo", admin: false, token: {}, grants };
  client.setQueryData(queryKeys.whoami, whoami);
  api.answers["/v1/auth/whoami"] = whoami;
  api.answers["/v1/me/overview"] = overview(grants);
  renderVi(
    <QueryClientProvider client={client}>
      <CommandPaletteProvider>
        <input aria-label="Lọc run" data-testid="outside-field" />
        <PaletteTrigger />
      </CommandPaletteProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

async function openPalette(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId("palette-trigger"));
  const palette = await screen.findByTestId("command-palette", {}, { timeout: 5_000 });
  await waitFor(() => expect(within(palette).getByRole("combobox")).toHaveFocus());
  return palette;
}

function items(palette: HTMLElement, group: string): string[] {
  const found = within(palette).queryByTestId(`palette-group-${group}`);
  return found ? within(found).getAllByRole("option").map((option) => option.getAttribute("data-item") ?? "") : [];
}

beforeEach(() => {
  api.answers = {
    "/v1/projects/demo/plans": [
      { ...PLAN, plan_id: "fleet", area: "active", title: "Plan runs on the web", steps_total: 5, steps_done: 2 },
      { ...PLAN, plan_id: "old", area: "completed", title: "Old plan", steps_total: 3, steps_done: 3 },
    ],
    "/v1/projects/docs/plans": [{ ...PLAN, plan_id: "guides", area: "active", title: "Guides", steps_total: 2, steps_done: 0 }],
    "/v1/workers": [
      { id: 1, name: "mini", hostname: "mini.local", owner: "octo", status: "online", held_runs: 1, slots: 1, projects: ["demo"], os: "macOS 15", arch: "arm64", runtimes: {}, created_at: "2026-10-01T00:00:00Z", last_heartbeat_at: "2026-10-07T01:00:00Z" },
      { id: 2, name: "docs-box", hostname: "docs.local", owner: "octo", status: "offline", held_runs: 0, slots: 1, projects: ["docs"], os: "Linux", arch: "x86_64", runtimes: {}, created_at: "2026-10-01T00:00:00Z", last_heartbeat_at: null },
    ],
  };
  api.seen = [];
  api.searches = [];
  api.gate = null;
  api.hold = {};
  nav.project = "demo";
  nav.push.mockReset();
});

describe("isPaletteShortcut", () => {
  const key = (extra: Partial<KeyboardEvent> & { target?: EventTarget | null }) =>
    ({ key: "k", code: "KeyK", metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, isComposing: false, target: document.body, ...extra }) as KeyboardEvent;

  it("takes Cmd K and Ctrl K with nothing else held", () => {
    expect(isPaletteShortcut(key({ metaKey: true }), true)).toBe(true);
    expect(isPaletteShortcut(key({ ctrlKey: true }), false)).toBe(true);
    expect(isPaletteShortcut(key({ ctrlKey: true, key: "K" }), false)).toBe(true);
    expect(isPaletteShortcut(key({ ctrlKey: true, key: "л" }), false)).toBe(true); // another layout, the same key
    expect(isPaletteShortcut(key({}), false)).toBe(false);
    expect(isPaletteShortcut(key({ ctrlKey: true, shiftKey: true }), false)).toBe(false);
    expect(isPaletteShortcut(key({ ctrlKey: true, metaKey: true }), false)).toBe(false);
    expect(isPaletteShortcut(key({ ctrlKey: true, key: "j", code: "KeyJ" }), false)).toBe(false);
  });

  it("leaves Ctrl K to a text field on an Apple keyboard, and the key to the web terminal", () => {
    const field = document.createElement("input");
    expect(isPaletteShortcut(key({ ctrlKey: true, target: field }), true)).toBe(false);
    expect(isPaletteShortcut(key({ metaKey: true, target: field }), true)).toBe(true);
    expect(isPaletteShortcut(key({ ctrlKey: true, target: field }), false)).toBe(true);
    const terminal = document.createElement("div");
    terminal.className = "hub-terminal";
    const inner = document.createElement("textarea");
    terminal.append(inner);
    expect(isPaletteShortcut(key({ ctrlKey: true, target: inner }), false)).toBe(false);
  });
});

describe("CommandPalette", () => {
  it("opens with Ctrl K and closes with Esc, giving focus back to the field that held it", async () => {
    const user = setup();
    expect(await screen.findByTestId("palette-trigger-key")).toHaveTextContent("Ctrl K");
    const field = screen.getByTestId("outside-field");
    await user.click(field);
    await user.keyboard("{Control>}k{/Control}");
    const palette = await screen.findByTestId("command-palette", {}, { timeout: 5_000 });
    expect(within(palette).getByRole("combobox", { name: "Tìm hoặc chạy lệnh" })).toHaveFocus();
    expect(screen.getByTestId("palette-trigger")).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("command-palette")).toBeNull());
    await waitFor(() => expect(field).toHaveFocus());

    // The top bar's field opens it too, and gets focus back.
    await openPalette(user);
    await user.keyboard("{Control>}k{/Control}");
    await waitFor(() => expect(screen.queryByTestId("command-palette")).toBeNull());
    await waitFor(() => expect(screen.getByTestId("palette-trigger")).toHaveFocus());
  });

  it("lists a writer's actions, then the runs, plans, workers and pages of the page's project", async () => {
    const user = setup();
    const palette = await openPalette(user);
    await waitFor(() => expect(items(palette, "actions")).toEqual(["action:plan-run:demo:fleet", "action:dispatch:demo", "action:rerun:demo:8", "action:register"]));
    expect(within(palette).getByRole("option", { name: /Chạy plan Plan runs on the web/ })).toHaveTextContent("còn 3 bước");
    expect(within(palette).getByRole("option", { name: /Chạy lại #8/ })).toHaveTextContent("thất bại");
    expect(items(palette, "runs")).toEqual(["run:demo:13", "run:demo:8"]);
    expect(items(palette, "plans")).toEqual(["plan:demo:fleet"]);
    await waitFor(() => expect(items(palette, "workers")).toEqual(["worker:1"]));
    expect(items(palette, "goto").slice(0, 3)).toEqual(["goto:demo:overview", "goto:demo:plans", "goto:demo:runs"]);
    expect(within(palette).getByRole("option", { name: /Run của demo/ })).toBeInTheDocument();
    expect(within(palette).getByTestId("palette-scope")).toHaveAttribute("data-scope", "demo");
    // Nothing that deletes or stops: no cancel, drain, revoke or sign out.
    expect(within(palette).queryByRole("option", { name: /Huỷ|Thu hồi|Ngừng|Đăng xuất/ })).toBeNull();
  });

  it("keeps the first item selected while groups load above it, until the person moves the selection", async () => {
    let release: () => void = () => {};
    api.hold["/v1/projects/demo/plans"] = new Promise<void>((resolve) => (release = resolve));
    const user = setup();
    const palette = await openPalette(user);
    await waitFor(() => expect(items(palette, "actions")).toEqual(["action:dispatch:demo", "action:rerun:demo:8", "action:register"]));
    const options = () => within(palette).getAllByRole("option");
    await waitFor(() => expect(options()[0]).toHaveAttribute("aria-selected", "true"));
    // The plans arrive: Run plan comes first, above Dispatch, and is the one selected.
    release();
    await waitFor(() => expect(items(palette, "actions")[0]).toBe("action:plan-run:demo:fleet"));
    expect(options()[0]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}");
    expect(options()[1]).toHaveAttribute("aria-selected", "true");
    expect(options()[1]).toHaveAttribute("data-item", "action:dispatch:demo");
    await user.keyboard("{End}");
    expect(options().at(-1)).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{Home}");
    expect(options()[0]).toHaveAttribute("aria-selected", "true");
  });

  it("offers a reader no action", async () => {
    const user = setup(READER);
    const palette = await openPalette(user);
    await waitFor(() => expect(items(palette, "runs").length).toBeGreaterThan(0));
    await waitFor(() => expect(items(palette, "plans")).toEqual(["plan:demo:fleet"]));
    expect(within(palette).queryByTestId("palette-group-actions")).toBeNull();
    expect(within(palette).queryByRole("option", { name: /Dispatch|Chạy plan|Chạy lại|Đăng ký worker/ })).toBeNull();
  });

  it("switches the project with Tab and Shift Tab, through every project", async () => {
    const user = setup();
    const palette = await openPalette(user);
    const scope = within(palette).getByTestId("palette-scope");
    await user.keyboard("{Tab}");
    expect(scope).toHaveAttribute("data-scope", "docs");
    expect(within(palette).getByRole("combobox")).toHaveFocus();
    // docs is read-only for octo: Register stays (they write in demo), Dispatch does not.
    await waitFor(() => expect(items(palette, "plans")).toEqual(["plan:docs:guides"]));
    expect(items(palette, "actions")).toEqual(["action:register"]);
    await user.keyboard("{Tab}");
    expect(scope).toHaveAttribute("data-scope", "");
    expect(scope).toHaveTextContent("Mọi dự án");
    await waitFor(() => expect(items(palette, "plans")).toEqual(["plan:demo:fleet", "plan:docs:guides"]));
    expect(items(palette, "actions")).toContain("action:dispatch:demo");
    expect(within(palette).getByRole("option", { name: /Dispatch một bước trong demo/ })).toBeInTheDocument();
    await user.keyboard("{Shift>}{Tab}{/Shift}");
    expect(scope).toHaveAttribute("data-scope", "docs");
    await user.click(scope);
    expect(scope).toHaveAttribute("data-scope", "");
    expect(within(palette).getByRole("combobox")).toHaveFocus();
  });

  it("asks the hub for the project's runs once typing pauses, and cancels the request a newer query replaces", async () => {
    const user = setup();
    const palette = await openPalette(user);
    await user.type(within(palette).getByRole("combobox"), "#4");
    await waitFor(() => expect(items(palette, "runs")).toEqual(["run:demo:42"]));
    // Typed faster than 150 ms apart: one request, for the whole query.
    expect(api.searches.map((search) => search.q)).toEqual(["#4"]);

    let open: () => void = () => {};
    api.gate = new Promise((resolve) => (open = resolve));
    await user.type(within(palette).getByRole("combobox"), "2");
    await waitFor(() => expect(api.searches.map((search) => search.q)).toEqual(["#4", "#42"]));
    await user.type(within(palette).getByRole("combobox"), "{Backspace}{Backspace}{Backspace}queue");
    await waitFor(() => expect(api.searches.at(-1)?.q).toBe("queue"));
    expect(api.searches.find((search) => search.q === "#42")?.signal.aborted).toBe(true);
    open();
    await waitFor(() => expect(items(palette, "runs")).toEqual(["run:demo:13"]));
  });

  it("goes to a run's page by its number, and says so when nothing matches", async () => {
    const user = setup();
    const palette = await openPalette(user);
    await user.type(within(palette).getByRole("combobox"), "#42");
    await waitFor(() => expect(items(palette, "runs")).toEqual(["run:demo:42"]));
    await user.keyboard("{Enter}");
    expect(nav.push).toHaveBeenCalledWith("/p/demo/runs/42");
    await waitFor(() => expect(screen.queryByTestId("command-palette")).toBeNull());

    const again = await openPalette(user);
    await user.type(within(again).getByRole("combobox"), "zzzz");
    expect(await within(again).findByTestId("palette-empty")).toHaveTextContent("Không có gì khớp “zzzz” trong demo.");
    expect(within(again).getByTestId("palette-count")).toHaveTextContent("Không có kết quả trong demo");
  });

  it("opens the dispatch dialog of the project with Dispatch a step", async () => {
    const user = setup();
    const palette = await openPalette(user);
    await user.type(within(palette).getByRole("combobox"), "dispatch");
    await waitFor(() => expect(items(palette, "actions")).toEqual(["action:dispatch:demo"]));
    await user.keyboard("{Enter}");
    expect(await screen.findByTestId("dispatch-dialog", {}, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.queryByTestId("command-palette")).toBeNull();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("dispatch-dialog")).toBeNull());
    await waitFor(() => expect(screen.getByTestId("palette-trigger")).toHaveFocus());
  });

  it("reruns the latest failed run as Home does, and says so in a toast", async () => {
    api.answers["/v1/projects/demo/runs/8/rerun"] = { ...run(15, "queued"), id: 15 };
    const user = setup();
    const palette = await openPalette(user);
    await user.type(within(palette).getByRole("combobox"), "rerun");
    await waitFor(() => expect(items(palette, "actions")).toEqual(["action:rerun:demo:8"]));
    await user.keyboard("{Enter}");
    await waitFor(() => expect(api.seen).toContain("POST /v1/projects/demo/runs/8/rerun"));
    expect(await screen.findByText("Đã dispatch run #15")).toBeInTheDocument();
  });
});
