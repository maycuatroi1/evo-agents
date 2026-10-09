// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { InboxBell } from "@/components/inbox/inbox-bell";
import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { AppSidebar } from "./app-sidebar";
import { fleetTone } from "./fleet-line";

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

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderVi(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <SidebarProvider defaultOpen>
          <AppSidebar />
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

describe("AppSidebar", () => {
  it("lists Home and Inbox, the project's areas and the hub's in the kit's order", async () => {
    setup();
    const navigation = screen.getByRole("navigation", { name: "Điều hướng chính" });
    const links = within(navigation).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("data-testid"))).toEqual([
      "nav-home",
      "nav-inbox",
      "nav-monitor",
      "nav-overview",
      "nav-plans",
      "nav-runs",
      "nav-insights",
      "nav-curator",
      "nav-memories",
      "nav-skills",
      "nav-kg",
      "nav-workers",
      "nav-secrets",
      "nav-myMemories",
      "nav-globalSkills",
    ]);
    expect(screen.getByTestId("nav-home")).toHaveAttribute("href", "/");
    // Administration only for a hub admin.
    expect(screen.queryByTestId("nav-admin")).toBeNull();
    // The page shown is marked, and its icon takes the brand colour through data-active.
    expect(screen.getByTestId("nav-runs")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("nav-runs")).toHaveAttribute("data-active", "true");
    expect(screen.getByTestId("nav-plans")).not.toHaveAttribute("aria-current");
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

  it("hides a count at zero, and the project's group outside a project", async () => {
    api.answers["/v1/me/notifications/count"] = { unread: 0, open_decisions: 0 };
    nav.pathname = "/inbox";
    nav.project = undefined;
    setup();
    await waitFor(() => expect(screen.getByTestId("inbox-bell")).toHaveAttribute("data-unread", "0"));
    expect(screen.queryByTestId("nav-inbox-count")).toBeNull();
    expect(screen.getByTestId("nav-inbox")).toHaveAccessibleName("Inbox");
    expect(screen.queryByTestId("nav-runs")).toBeNull();
    expect(screen.getByTestId("nav-inbox")).toHaveAttribute("aria-current", "page");
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
