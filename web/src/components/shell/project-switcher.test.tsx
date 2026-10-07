// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SidebarProvider } from "@/components/ui/sidebar";
import { TooltipProvider } from "@/components/ui/tooltip";
import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { matchProjects, ProjectSwitcher, switcherProjects } from "./project-switcher";

const api = vi.hoisted(() => ({ answers: {} as Record<string, unknown>, seen: [] as string[] }));
const nav = vi.hoisted(() => ({ project: "beta" as string | undefined }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        api.seen.push(path);
        if (!(path in api.answers)) return Response.json({ detail: "not found" }, { status: 404 });
        return Response.json(api.answers[path]);
      },
    }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => (nav.project ? `/p/${nav.project}` : "/"),
  useParams: () => (nav.project ? { project: nav.project } : {}),
}));

const repo = { name: "api", origin: "https://github.com/example-org/api", default_branch: "main", path: "api" };

const PROJECTS = [
  { name: "alpha-api", role: "writer", max_level: "internal", repos: [repo, repo] },
  { name: "beta", role: "reader", max_level: "public", repos: [repo] },
  { name: "hidden-ops", role: null, max_level: null, repos: [] },
];

const OVERVIEW = {
  projects: [
    { name: "alpha-api", role: "writer", max_level: "internal", repos: 2, active_plans: 3, open_decisions: 2 },
    { name: "beta", role: "reader", max_level: "public", repos: 1, active_plans: 0, open_decisions: 0 },
  ],
};

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <SidebarProvider defaultOpen>
          <ProjectSwitcher />
        </SidebarProvider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  api.answers = { "/v1/projects": PROJECTS, "/v1/me/overview": OVERVIEW };
  api.seen = [];
  nav.project = "beta";
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

describe("switcherProjects and matchProjects", () => {
  it("joins the projects the visitor can open with the overview's counts, where it has them", () => {
    const rows = switcherProjects(PROJECTS, OVERVIEW.projects);
    expect(rows).toEqual([
      { name: "alpha-api", role: "writer", maxLevel: "internal", repos: 2, activePlans: 3, openDecisions: 2 },
      { name: "beta", role: "reader", maxLevel: "public", repos: 1, activePlans: 0, openDecisions: 0 },
      { name: "hidden-ops", role: null, maxLevel: null, repos: 0, activePlans: null, openDecisions: null },
    ]);
    expect(switcherProjects(PROJECTS, undefined)[0]).toMatchObject({ repos: 2, activePlans: null, openDecisions: null });
  });

  it("keeps the projects whose name holds every word typed", () => {
    const rows = switcherProjects(PROJECTS, undefined);
    expect(matchProjects(rows, "").map((row) => row.name)).toEqual(["alpha-api", "beta", "hidden-ops"]);
    expect(matchProjects(rows, "  API  ").map((row) => row.name)).toEqual(["alpha-api"]);
    expect(matchProjects(rows, "p").map((row) => row.name)).toEqual(["alpha-api", "hidden-ops"]);
    expect(matchProjects(rows, "ops p").map((row) => row.name)).toEqual(["hidden-ops"]);
    expect(matchProjects(rows, "zzz")).toEqual([]);
  });
});

describe("ProjectSwitcher", () => {
  it("names the current project, and reads the counts only once it opens", async () => {
    setup();
    const trigger = screen.getByTestId("project-switcher");
    await waitFor(() => expect(trigger).toHaveAccessibleName("Chọn dự án, đang chọn beta"));
    expect(api.seen).not.toContain("/v1/me/overview");
    await userEvent.click(trigger);
    await waitFor(() => expect(api.seen).toContain("/v1/me/overview"));
  });

  it("lists every project with its role, Visibility, repos, active plans and open decisions", async () => {
    const user = setup();
    await user.click(screen.getByTestId("project-switcher"));
    const menu = await screen.findByRole("dialog", { name: "Chọn dự án" });
    expect(within(menu).getByRole("searchbox", { name: "Tìm dự án theo tên" })).toHaveFocus();
    const list = within(menu).getByRole("navigation", { name: "Dự án" });
    const links = within(list).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("data-project"))).toEqual(["alpha-api", "beta", "hidden-ops"]);
    await waitFor(() => expect(links[0]).toHaveTextContent("Ghi, Mức hiển thị: Internal, 2 kho mã, 3 plan đang chạy"));
    expect(within(links[0]).getByTestId("switcher-project-decisions")).toHaveTextContent("22 quyết định đang mở");
    expect(links[0]).toHaveAttribute("href", "/p/alpha-api");
    expect(links[1]).toHaveAttribute("aria-current", "true");
    expect(links[1]).toHaveTextContent("Đọc, Mức hiển thị: Public, 1 kho mã, không có plan đang chạy");
    // A hub admin's project without a grant: no role, and no counts the overview could give.
    expect(links[2]).toHaveTextContent("Không có vai trò, 0 kho mã");
    expect(within(menu).getByTestId("project-switcher-home")).toHaveAttribute("href", "/");
  });

  it("filters by name as the visitor types, moves through the results with the arrow keys, and empties the field on Escape", async () => {
    const user = setup();
    await user.click(screen.getByTestId("project-switcher"));
    const menu = await screen.findByRole("dialog", { name: "Chọn dự án" });
    const field = within(menu).getByRole("searchbox");
    await user.type(field, "zz");
    expect(within(menu).getByTestId("project-switcher-no-match")).toHaveTextContent("Không dự án nào khớp “zz”.");
    expect(within(menu).getByTestId("project-switcher-count")).toHaveTextContent("Không dự án nào khớp");
    await user.clear(field);
    await user.type(field, "a");
    const links = within(menu).getAllByRole("link", { name: /^(alpha-api|beta|hidden-ops)/ });
    expect(links.map((link) => link.getAttribute("data-project"))).toEqual(["alpha-api", "beta"]);
    await user.type(field, "pi");
    expect(within(menu).getAllByRole("link").map((link) => link.getAttribute("data-project")).filter(Boolean)).toEqual(["alpha-api"]);
    expect(within(menu).getByTestId("project-switcher-count")).toHaveTextContent("1 dự án");

    await user.keyboard("{ArrowDown}");
    expect(within(menu).getByRole("link", { name: /^alpha-api/ })).toHaveFocus();
    await user.keyboard("{ArrowUp}");
    expect(field).toHaveFocus();

    // The first Escape empties the field, the second closes the switcher.
    await user.keyboard("{Escape}");
    expect(field).toHaveValue("");
    expect(screen.getByRole("dialog", { name: "Chọn dự án" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Chọn dự án" })).toBeNull());
  });
});
