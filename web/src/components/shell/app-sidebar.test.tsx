// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { InboxBell } from "@/components/inbox/inbox-bell";
import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { AppSidebar } from "./app-sidebar";
import { fleetTone } from "./fleet-line";
import type { NavGroup } from "./nav";

type Answers = Record<string, unknown>;

const api = vi.hoisted(() => ({ answers: {} as Record<string, unknown>, fail: new Set<string>() }));
const nav = vi.hoisted(() => ({ pathname: "/p/demo/runs", project: "demo" as string | undefined }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        if (api.fail.has(path)) return Response.json({ detail: "boom" }, { status: 500 });
        if (!(path in api.answers)) return Response.json({ detail: "not found" }, { status: 404 });
        return Response.json(api.answers[path]);
      },
    }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useParams: () => (nav.project ? { project: nav.project } : {}),
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

const COUNTS = {
  queued: 1,
  leased: 0,
  running: 1,
  interactive: 0,
  verifying: 0,
  waiting: 1,
  review: 0,
  parked: 0,
  done: 12,
  failed: 2,
  lost: 1,
  cancelled: 0,
};

function worker(id: number, status: string, held_runs: number) {
  return { id, name: `w${id}`, status, held_runs, last_heartbeat_at: "2026-10-06T08:00:00Z", created_at: "2026-10-01T00:00:00Z" };
}

function baseAnswers(): Answers {
  return {
    "/v1/auth/whoami": { login: "octo", admin: false, grants: [{ project: "demo" }] },
    "/v1/projects": [{ name: "demo", role: "writer", max_level: "internal" }],
    "/v1/me/notifications/count": { unread: 4, open_decisions: 2 },
    "/v1/projects/demo/runs": { runs: [], total: 3, counts: COUNTS, limit: 200, offset: 0 },
    "/v1/workers": [worker(1, "online", 1), worker(2, "online", 0), worker(3, "offline", 0), worker(4, "revoked", 0)],
  };
}

let client: QueryClient;

function setup({ openGroups = [], folded = false }: { openGroups?: NavGroup[]; folded?: boolean } = {}) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderVi(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <SidebarProvider defaultOpen={!folded}>
          <AppSidebar openGroups={openGroups} />
          <InboxBell />
        </SidebarProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.answers = baseAnswers();
  api.fail = new Set();
  nav.pathname = "/p/demo/runs";
  nav.project = "demo";
  window.localStorage.clear();
  document.cookie = "hub_nav_groups=; path=/; max-age=0";
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
  // The folded sidebar's fold menu measures its trigger.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

describe("AppSidebar", () => {
  it("lists Home and Inbox, the project's areas and the hub's in the kit's order", async () => {
    setup();
    const navigation = screen.getByRole("navigation", { name: "Điều hướng chính" });
    const links = within(navigation).getAllByRole("link", { hidden: true });
    expect(links.map((link) => link.getAttribute("data-testid"))).toEqual([
      "nav-home",
      "nav-inbox",
      "nav-monitor",
      "nav-chats",
      "nav-kg",
      "nav-overview",
      "nav-decide",
      "nav-plans",
      "nav-runs",
      "nav-mergeQueue",
      "nav-releases",
      "nav-problems",
      "nav-opportunities",
      "nav-goals",
      "nav-signals",
      "nav-loop",
      "nav-insights",
      "nav-curator",
      "nav-agents",
      "nav-memories",
      "nav-skills",
      "nav-browser",
      "nav-plugins",
      "nav-workers",
      "nav-org",
      "nav-myMemories",
      "nav-globalSkills",
      "nav-secrets",
      "nav-accounts",
    ]);
    expect(screen.getByTestId("nav-home")).toHaveAttribute("href", "/");
    // Administration only for a hub admin.
    expect(screen.queryByTestId("nav-admin")).toBeNull();
    // The page shown is marked, and its icon takes the brand colour through data-active.
    expect(screen.getByTestId("nav-runs")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("nav-runs")).toHaveAttribute("data-active", "true");
    expect(screen.getByTestId("nav-plans")).not.toHaveAttribute("aria-current");
  });

  it("keeps the areas opened less often in closed folds, so the sidebar shows the daily ones and a row per fold", () => {
    setup();
    const navigation = screen.getByRole("navigation", { name: "Điều hướng chính" });
    expect(within(navigation).getAllByRole("link").map((link) => link.getAttribute("data-testid"))).toEqual([
      "nav-home",
      "nav-inbox",
      "nav-monitor",
      "nav-chats",
      "nav-kg",
      "nav-overview",
      "nav-decide",
      "nav-plans",
      "nav-runs",
      // Apps holds Plugins alone while no plugin adds a page: a fold of one shows its item on its own row.
      "nav-plugins",
      "nav-workers",
      "nav-org",
      "nav-myMemories",
      "nav-globalSkills",
    ]);
    const folds = within(navigation).getAllByRole("button");
    expect(folds.map((fold) => [fold.getAttribute("data-testid"), fold.textContent, fold.getAttribute("aria-expanded")])).toEqual([
      ["nav-group-delivery", "Bàn giao", "false"],
      ["nav-group-improvement", "Cải tiến", "false"],
      ["nav-group-knowledge", "Tri thức", "false"],
      ["nav-group-settings", "Cài đặt", "false"],
    ]);
    expect(screen.getByTestId("nav-curator")).not.toBeVisible();
  });

  it("opens a fold on a click and keeps it open in a cookie, for the next page the server renders", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByTestId("nav-group-improvement"));
    expect(screen.getByTestId("nav-group-improvement")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("nav-curator")).toBeVisible();
    expect(document.cookie).toContain("hub_nav_groups=improvement");
    await user.click(screen.getByTestId("nav-group-knowledge"));
    expect(document.cookie).toContain(`hub_nav_groups=${encodeURIComponent("improvement,knowledge")}`);
    await user.click(screen.getByTestId("nav-group-improvement"));
    expect(screen.getByTestId("nav-curator")).not.toBeVisible();
    expect(document.cookie).toContain("hub_nav_groups=knowledge");
  });

  it("renders the folds the cookie names open", () => {
    setup({ openGroups: ["delivery", "settings"] });
    expect(screen.getByTestId("nav-group-delivery")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("nav-releases")).toBeVisible();
    expect(screen.getByTestId("nav-secrets")).toBeVisible();
    expect(screen.getByTestId("nav-group-improvement")).toHaveAttribute("aria-expanded", "false");
  });

  it("opens the fold of the page shown, and marks its row when the visitor closes it there", async () => {
    const user = userEvent.setup();
    nav.pathname = "/p/demo/curator/history";
    setup();
    expect(screen.getByTestId("nav-group-improvement")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("nav-curator")).toBeVisible();
    expect(screen.getByTestId("nav-curator")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("nav-group-improvement")).toHaveAttribute("data-active", "false");
    // Opened by the page, not by the visitor: nothing is kept.
    expect(document.cookie).not.toContain("hub_nav_groups=improvement");
    await user.click(screen.getByTestId("nav-group-improvement"));
    expect(screen.getByTestId("nav-curator")).not.toBeVisible();
    expect(screen.getByTestId("nav-group-improvement")).toHaveAttribute("data-active", "true");
  });

  it("opens a fold's items in a menu beside its icon on the folded sidebar", async () => {
    const user = userEvent.setup();
    nav.pathname = "/p/demo/signals";
    setup({ folded: true });
    const fold = screen.getByTestId("nav-group-improvement");
    expect(fold).toHaveAttribute("data-active", "true");
    expect(screen.queryByTestId("nav-signals")).toBeNull();
    await user.click(fold);
    const menu = await screen.findByRole("menu");
    expect(within(menu).getAllByRole("menuitem").map((item) => item.getAttribute("data-testid"))).toEqual([
      "nav-problems",
      "nav-opportunities",
      "nav-goals",
      "nav-signals",
      "nav-loop",
      "nav-insights",
      "nav-curator",
    ]);
    expect(within(menu).getByTestId("nav-signals")).toHaveAttribute("aria-current", "page");
    expect(within(menu).getByTestId("nav-signals")).toHaveAttribute("href", "/p/demo/signals");
  });

  it("lists the pages of the project's enabled plugins before its Plugins, each with its manifest's title", async () => {
    const page = (name: string, extra = {}) => ({ name, title: `Page ${name}`, description: "", icon: "kanban", menu: true, native: false, url: `/v1/plugin-ui/x/demo/1.a/${name}.html`, ...extra });
    const plugin = (name: string, extra = {}) => ({
      name,
      title: name,
      description: "",
      tier: "isolated",
      publisher: "acme",
      version: "1.0.0",
      state: "installed",
      disabled_reason: null,
      enabled: true,
      permissions: [],
      granted: [],
      pending: [],
      config_schema: null,
      pages: [page("board"), page("hidden", { menu: false })],
      enabled_by: null,
      enabled_at: null,
      granted_by: null,
      granted_at: null,
      ...extra,
    });
    api.answers["/v1/projects/demo/plugins"] = [
      plugin("sample"),
      plugin("notes", { tier: "core", pages: [page("entries", { icon: "notebook-pen", native: true, url: null })] }),
      plugin("off", { enabled: false }),
      plugin("broken", { state: "disabled", disabled_reason: "incompatible" }),
    ];
    nav.pathname = "/p/demo/x/sample/board";
    setup();
    const board = await screen.findByTestId("nav-plugin-sample-board");
    expect(board).toHaveAttribute("href", "/p/demo/x/sample/board");
    expect(board).toHaveAttribute("aria-current", "page");
    expect(board).toHaveTextContent("Page board");
    // In the Apps fold, open by itself on a plugin's page.
    expect(screen.getByTestId("nav-group-apps")).toHaveAttribute("aria-expanded", "true");
    expect(board).toBeVisible();
    const links = within(screen.getByRole("navigation", { name: "Điều hướng chính" })).getAllByRole("link", { hidden: true });
    const ids = links.map((link) => link.getAttribute("data-testid"));
    expect(ids.slice(ids.indexOf("nav-browser"), ids.indexOf("nav-plugins") + 1)).toEqual([
      "nav-browser",
      "nav-plugin-sample-board",
      "nav-plugin-notes-entries",
      "nav-plugins",
    ]);
  });

  it("leaves out the areas a hub admin turned off and shows the name and logo of the hub's branding", async () => {
    api.answers["/v1/features"] = {
      features: [
        { id: "chats", layers: ["instance"], enabled: false },
        { id: "curator", layers: ["instance", "project"], enabled: false },
        { id: "kg", layers: ["instance", "project"], enabled: true },
        { id: "monitor", layers: ["instance"], enabled: false },
      ],
      projects: [{ project: "demo", values: { kg: false, curator: true } }],
    };
    api.answers["/v1/branding"] = {
      name: "Acme Agents",
      logo_url: "/v1/branding/logo?v=0123456789abcdef",
      favicon_url: null,
      colors: { light: {}, dark: {} },
      version: 2,
    };
    setup();
    await waitFor(() => expect(screen.queryByTestId("nav-kg")).toBeNull());
    for (const gone of ["nav-chats", "nav-monitor"]) expect(screen.queryByTestId(gone)).toBeNull();
    expect(screen.getByTestId("nav-curator")).toBeInTheDocument(); // the project's layer turns it back on
    expect(screen.getByTestId("brand")).toHaveTextContent("Acme Agents");
    expect(screen.getByTestId("hub-logo")).toHaveAttribute("src", "/v1/branding/logo?v=0123456789abcdef");
  });

  it("counts open decisions in attention and active runs in running, in words for screen readers", async () => {
    setup();
    await waitFor(() => expect(screen.getByTestId("nav-inbox-count")).toHaveTextContent("2"));
    expect(screen.getByTestId("nav-inbox-count").className).toContain("bg-attention-soft");
    expect(screen.getByTestId("nav-inbox")).toHaveAccessibleName("Inbox, 2 quyết định hoặc đề xuất chờ bạn trả lời");
    await waitFor(() => expect(screen.getByTestId("nav-runs-count")).toHaveTextContent("3"));
    expect(screen.getByTestId("nav-runs-count").className).toContain("bg-running-soft");
    expect(screen.getByTestId("nav-runs")).toHaveAccessibleName("Run, 3 run đang hoạt động");
  });

  it("counts the unread messages of the visitor's chats on Chats, in attention, and hides the count at zero", async () => {
    api.answers["/v1/chats"] = [
      { id: 1, title: "Release", unread: 2, people: ["octo"], agents: ["reviewer"] },
      { id: 2, title: "Docs", unread: 3, people: ["octo"], agents: [] },
    ];
    const { unmount } = setup();
    await waitFor(() => expect(screen.getByTestId("nav-chats-count")).toHaveTextContent("5"));
    expect(screen.getByTestId("nav-chats-count").className).toContain("bg-attention-soft");
    expect(screen.getByTestId("nav-chats")).toHaveAttribute("href", "/chats");
    expect(screen.getByTestId("nav-chats")).toHaveAccessibleName("Chat, 5 tin chat chưa đọc");
    unmount();
    api.answers["/v1/chats"] = [{ id: 1, title: "Release", unread: 0, people: ["octo"], agents: [] }];
    setup();
    await waitFor(() => expect(screen.getByTestId("nav-inbox-count")).toBeInTheDocument());
    expect(screen.queryByTestId("nav-chats-count")).toBeNull();
    expect(screen.getByTestId("nav-chats")).toHaveAccessibleName("Chat");
  });

  it("counts the runs at work in every project on Monitor, in running, and hides the count at zero", async () => {
    api.answers["/v1/me/overview"] = { counts: { running: 3 } };
    setup();
    await waitFor(() => expect(screen.getByTestId("nav-monitor-count")).toHaveTextContent("3"));
    expect(screen.getByTestId("nav-monitor-count").className).toContain("bg-running-soft");
    expect(screen.getByTestId("nav-monitor")).toHaveAttribute("href", "/monitor");
    expect(screen.getByTestId("nav-monitor")).toHaveAccessibleName("Monitor, 3 run đang chạy");
  });

  it("shows no Monitor count while nothing runs, or while the overview cannot be read", async () => {
    api.answers["/v1/me/overview"] = { counts: { running: 0 } };
    const { unmount } = setup();
    await waitFor(() => expect(screen.getByTestId("nav-inbox-count")).toBeInTheDocument());
    expect(screen.queryByTestId("nav-monitor-count")).toBeNull();
    expect(screen.getByTestId("nav-monitor")).toHaveAccessibleName("Monitor");
    unmount();
    api.fail.add("/v1/me/overview");
    setup();
    await waitFor(() => expect(screen.getByTestId("nav-inbox-count")).toBeInTheDocument());
    expect(screen.queryByTestId("nav-monitor-count")).toBeNull();
  });

  it("hides a count at zero, and outside a project shows the group of the project the switcher stands in with", async () => {
    api.answers["/v1/me/notifications/count"] = { unread: 0, open_decisions: 0 };
    nav.pathname = "/inbox";
    nav.project = undefined;
    setup();
    await waitFor(() => expect(screen.getByTestId("inbox-bell")).toHaveAttribute("data-unread", "0"));
    expect(screen.queryByTestId("nav-inbox-count")).toBeNull();
    expect(screen.getByTestId("nav-inbox")).toHaveAccessibleName("Inbox");
    expect(screen.getByTestId("nav-inbox")).toHaveAttribute("aria-current", "page");
    expect(await screen.findByTestId("nav-runs")).toHaveAttribute("href", "/p/demo/runs");
    expect(screen.getByTestId("nav-runs")).not.toHaveAttribute("aria-current");
  });

  it("shows no project's group outside a project when the visitor can open none", async () => {
    api.answers["/v1/projects"] = [];
    nav.pathname = "/inbox";
    nav.project = undefined;
    setup();
    await waitFor(() => expect(client.getQueryState(queryKeys.projects)?.status).toBe("success"));
    expect(screen.queryByTestId("nav-runs")).toBeNull();
  });

  it("shows the fleet line from the workers list, linked to the Workers page", async () => {
    setup();
    const line = await screen.findByTestId("fleet-line");
    expect(line).toHaveAttribute("href", "/workers");
    expect(line).toHaveAttribute("data-online", "2");
    expect(line).toHaveAttribute("data-busy", "1");
    expect(line).toHaveAttribute("data-tone", "success");
    expect(line).toHaveTextContent("2 worker online, 1 đang chạy");
  });

  it("says when no worker is online, and when the workers cannot be read", async () => {
    api.answers["/v1/workers"] = [worker(3, "offline", 0), worker(5, "draining", 0)];
    const first = setup();
    const line = await screen.findByTestId("fleet-line");
    expect(line).toHaveAttribute("data-tone", "danger");
    expect(line).toHaveTextContent("Không có worker nào online, 2 đã đăng ký");
    first.unmount();

    api.fail.add("/v1/workers");
    setup();
    await waitFor(() => expect(screen.getByTestId("fleet-line")).toHaveTextContent("Không đọc được danh sách worker"));
    expect(screen.getByTestId("fleet-line")).toHaveAttribute("data-tone", "neutral");
  });

  it("raises the bell's count beside the glyph in attention, not over it", async () => {
    setup();
    const count = await screen.findByTestId("inbox-bell-count");
    expect(count).toHaveTextContent("4");
    expect(count.className).toContain("bg-attention-soft");
    expect(count.className).not.toContain("absolute");
    expect(screen.getByTestId("inbox-bell-glyph").nextElementSibling).toBe(count);
  });
});

describe("fleetTone", () => {
  it("is success while a worker is online, danger when none is, neutral before the first", () => {
    expect(fleetTone({ registered: 3, online: 1, busy: 0 })).toBe("success");
    expect(fleetTone({ registered: 2, online: 0, busy: 0 })).toBe("danger");
    expect(fleetTone({ registered: 0, online: 0, busy: 0 })).toBe("neutral");
  });
});
