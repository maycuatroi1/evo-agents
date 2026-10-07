// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { sectionActive } from "./admin-nav";
import { adminCrumbs } from "./crumbs";
import { type AdminOverview, adminKeys } from "./data";
import { AdminDiagnosticsPage } from "./diagnostics-page";
import { attentionItems, failedBuildsHref, membersHref, tableArea, tableRows } from "./overview-model";
import { AdminOverviewPage } from "./overview-page";

vi.mock("next/navigation", () => ({ usePathname: () => "/admin", useRouter: () => ({ push: vi.fn() }) }));

beforeAll(() => {
  // Recharts measures its container; jsdom has no layout.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

const QUIET: AdminOverview = {
  members: { total: 4, active: 4, not_signed_in: 0, active_days: 30 },
  tokens: { live: 6, expiring: 0, unused: 0, expiring_days: 14, unused_days: 90 },
  grants: [],
  storage: { objects: 0, bytes: 0, pending_deletions: 0, pending_bytes: 0 },
  kg_builds: { failed: 0, days: 7, projects: [] },
  workers: { live: 2, offline: 0, offline_after_seconds: 300 },
  audit: { rows: 0, hours: 24 },
};

const BUSY: AdminOverview = {
  members: { total: 12, active: 8, not_signed_in: 2, active_days: 30 },
  tokens: { live: 1234, expiring: 3, unused: 5, expiring_days: 14, unused_days: 90 },
  grants: [
    { project: "evo-agents", admins: 1, writers: 2, readers: 1 },
    { project: "lonely", admins: 0, writers: 0, readers: 0 },
  ],
  storage: { objects: 4210, bytes: 1.5 * 1024 ** 3, pending_deletions: 2, pending_bytes: 3 * 1024 * 1024 },
  kg_builds: {
    failed: 3,
    days: 7,
    projects: [
      {
        project: "fixed",
        failed: 1,
        last_failed_id: 40,
        last_failed_at: "2026-10-06T01:00:00Z",
        latest_id: 41,
        latest_status: "succeeded",
      },
      {
        project: "evo-agents",
        failed: 2,
        last_failed_id: 52,
        last_failed_at: "2026-10-05T01:00:00Z",
        latest_id: 52,
        latest_status: "failed",
      },
    ],
  },
  workers: { live: 3, offline: 1, offline_after_seconds: 300 },
  audit: { rows: 2500, hours: 24 },
};

function renderWith(ui: ReactElement, key: readonly unknown[], data: unknown) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(key, data);
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("what needs an admin", () => {
  it("lists the pressing things first, each with the page that lists it filtered, and leaves zeros out", () => {
    const items = attentionItems(BUSY);
    expect(items.map((item) => [item.kind, item.tone, item.href])).toEqual([
      ["builds", "danger", "/p/evo-agents/kg?builds=failed#build-history"],
      ["offline", "danger", "/workers?status=offline"],
      ["expiring", "attention", "/admin/tokens?state=expiring"],
      ["builds", "neutral", "/p/fixed/kg?builds=failed#build-history"],
      ["unused", "neutral", "/admin/tokens?state=unused"],
      ["notSignedIn", "neutral", "/admin/members"],
      ["deletions", "neutral", null],
    ]);
    expect(attentionItems(QUIET)).toEqual([]);
  });

  it("links a project's members and failed builds by its name", () => {
    expect(membersHref("evo-agents")).toBe("/admin/members?project=evo-agents");
    expect(membersHref()).toBe("/admin/members");
    expect(failedBuildsHref("evo-agents")).toBe("/p/evo-agents/kg?builds=failed#build-history");
  });
});

describe("the overview page", () => {
  it("shows the hub at a glance, each cell opening its page, then what needs attention and every project's access", () => {
    renderWith(<AdminOverviewPage initialError={null} />, adminKeys.overview, BUSY);
    const strip = screen.getByTestId("admin-summary");
    expect(within(strip).getByTestId("summary-tokens-value")).toHaveTextContent("1.234");
    expect(within(strip).getByTestId("summary-storage-value")).toHaveTextContent("1,5 GB");
    expect(within(strip).getByTestId("summary-tokens")).toHaveTextContent("3 token hết hạn trong 14 ngày tới");
    expect(within(strip).getByTestId("summary-workers")).toHaveTextContent("1 offline");
    const links = Object.fromEntries(
      ["members", "tokens", "workers", "storage", "audit"].map((id) => [id, within(strip).getByTestId(`summary-${id}-link`).getAttribute("href")]),
    );
    expect(links).toEqual({
      members: "/admin/members",
      tokens: "/admin/tokens",
      workers: "/workers",
      storage: "/admin/diagnostics",
      audit: "/admin/audit",
    });
    // The cell's link is named by its label and described by the figure and its line.
    const tokens = screen.getByRole("link", { name: "Token còn hiệu lực" });
    expect(tokens).toHaveAccessibleDescription(/1\.234.*3 token hết hạn/);

    const attention = screen.getByTestId("admin-attention");
    expect(within(attention).getByRole("heading", { level: 2, name: "Cần xử lý" })).toBeInTheDocument();
    expect(within(attention).getByTestId("admin-attention-count")).toHaveTextContent("3"); // danger and attention rows
    const rows = within(attention).getAllByTestId("attention-item");
    expect(rows.map((row) => row.dataset.kind)).toEqual(["builds", "offline", "expiring", "builds", "unused", "notSignedIn", "deletions"]);
    expect(rows[0]).toHaveTextContent("2 lần build đồ thị lỗi ở evo-agents");
    expect(within(rows[0]).getByRole("link")).toHaveAttribute("href", "/p/evo-agents/kg?builds=failed#build-history");
    expect(rows[3]).toHaveTextContent("Build mới nhất #41 đã thành công");
    expect(rows[6]).toHaveTextContent("Tổng 3 MB; lần dọn tiếp theo sẽ thử lại");
    expect(within(rows[6]).queryByRole("link")).toBeNull(); // no page lists pending deletions

    const access = within(screen.getByTestId("admin-access")).getAllByTestId("access-project");
    expect(access[0]).toHaveTextContent("1 người quản trị, 2 người ghi, 1 người đọc");
    expect(within(access[0]).getByRole("link", { name: "evo-agents" })).toHaveAttribute("href", "/admin/members?project=evo-agents");
    expect(access[1]).toHaveTextContent("Chưa ai có quyền");
    expect(screen.getByTestId("admin-diagnostics-link")).toHaveAttribute("href", "/admin/diagnostics");
    // No table name on the overview.
    expect(screen.queryByText(/blob_deletions|kg_pending_runs/)).toBeNull();
  });

  it("says all is clear when nothing needs an admin", () => {
    renderWith(<AdminOverviewPage initialError={null} />, adminKeys.overview, QUIET);
    expect(screen.getByTestId("admin-all-clear")).toHaveTextContent("Mọi thứ ổn.");
    expect(screen.queryByTestId("attention-item")).toBeNull();
    expect(screen.queryByTestId("admin-attention-count")).toBeNull();
    expect(screen.getByTestId("admin-access")).toHaveTextContent("Chưa có dự án nào được đăng ký");
  });
});

describe("diagnostics", () => {
  it("files every table under an area of the hub, by its name", () => {
    expect(tableArea("users")).toBe("access");
    expect(tableArea("project_sinks")).toBe("projects");
    expect(tableArea("memory_revisions")).toBe("content");
    expect(tableArea("worker_pairings")).toBe("runs");
    expect(tableArea("run_events")).toBe("runs");
    expect(tableArea("decisions")).toBe("runs");
    expect(tableArea("credential_leases")).toBe("credentials");
    expect(tableArea("notification_deliveries")).toBe("notifications");
    expect(tableArea("kg_pending_runs")).toBe("graph");
    expect(tableArea("blob_deletions")).toBe("storage");
    expect(tableArea("procrastinate_jobs")).toBe("queue");
    expect(tableArea("something_new")).toBe("other");
    expect(tableRows({ blobs: 3, users: 2 })).toEqual({
      rows: [
        { table: "blobs", area: "storage", rows: 3 },
        { table: "users", area: "access", rows: 2 },
      ],
      total: 5,
    });
  });

  it("lists every table with its rows and the totals", () => {
    renderWith(<AdminDiagnosticsPage initialError={null} />, adminKeys.stats, { audit: 2500, blob_deletions: 4, users: 12 });
    expect(screen.getByRole("heading", { level: 1, name: "Chẩn đoán" })).toBeInTheDocument();
    const table = screen.getByTestId("diagnostics-table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      "auditNgười dùng và quyền2.500",
      "blob_deletionsLưu trữ blob4",
      "usersNgười dùng và quyền12",
    ]);
    expect(screen.getByTestId("diagnostics-total")).toHaveTextContent("3 bảng, tổng 2.516 dòng");
  });
});

describe("the admin area's navigation", () => {
  it("keeps the overview's tab selected on the pages it links to", () => {
    const overview = { id: "overview", href: "/admin" } as const;
    const members = { id: "members", href: "/admin/members" } as const;
    expect(sectionActive("/admin", overview)).toBe(true);
    expect(sectionActive("/admin/diagnostics", overview)).toBe(true);
    expect(sectionActive("/admin/members", overview)).toBe(false);
    expect(sectionActive("/admin/members/octo", members)).toBe(true);
    expect(sectionActive("/admin/diagnostics", members)).toBe(false);
  });

  it("names diagnostics in the breadcrumb under the admin area", () => {
    const label = (section: string) => `[${section}]`;
    expect(adminCrumbs(["diagnostics"], "Admin", label)).toEqual([{ label: "Admin", href: "/admin" }, { label: "[diagnostics]" }]);
  });
});
