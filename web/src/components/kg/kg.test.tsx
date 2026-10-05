// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiErrorInfo } from "@/lib/api/errors";
import { kgKeys } from "@/lib/kg/queries";
import type { GraphNode, KgBuild, Neighbourhood } from "@/lib/kg/types";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { BuildStatusBadge } from "./badges";
import { BuildHistory, LatestBuild, latestSucceeded } from "./build-status";
import { KgNodePage } from "./kg-node-page";
import { KgOverview } from "./kg-overview";
import { NeighbourhoodSection } from "./neighbourhood";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/p/demo/kg" }));

const label = { level: "internal", location: "any", integrity: "U" };
const node = (id: string, hop: number, name: string, kind = "Section"): GraphNode => ({
  id,
  kind,
  name,
  status: "parsed",
  conf: 1,
  label,
  props: {},
  aliases: [],
  hop,
  hub: false,
});

const VIEW: Neighbourhood = {
  graph: { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 4, edges: 3, finished_at: "2026-10-04T10:00:00Z", latest: true },
  focus: "requirement:KB-01",
  hops: 2,
  limit: 150,
  nodes: [
    node("requirement:KB-01", 0, "KB-01", "Requirement"),
    node("docs:doc:runbook#cài-đặt", 1, "Cài đặt"),
    node("docs:doc:runbook#sổ-tay", 1, "Sổ tay vận hành"),
    node("docs:doc:runbook", 2, "runbook", "Document"),
  ],
  edges: [
    { id: "e1", src: "docs:doc:runbook#cài-đặt", rel: "mentions", dst: "requirement:KB-01", status: "parsed" },
    { id: "e2", src: "docs:doc:runbook#sổ-tay", rel: "mentions", dst: "requirement:KB-01", status: "parsed" },
    { id: "e3", src: "docs:doc:runbook#cài-đặt", rel: "part_of", dst: "docs:doc:runbook", status: "parsed" },
  ],
  neighbours: 2,
  left_out: 0,
  truncated: false,
};

function withQueries(ui: ReactElement, seed: (client: ReturnType<typeof makeQueryClient>) => void = () => {}) {
  const client = makeQueryClient();
  seed(client);
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const failure = (status: number): ApiErrorInfo => ({
  status,
  code: status === 403 ? "forbidden" : status === 404 ? "not_found" : "unavailable",
  message: "from the API",
  requestId: "rid-77",
});

function narrowScreen(matches: boolean) {
  window.matchMedia = ((query: string) => ({
    matches,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
}

beforeEach(() => narrowScreen(true));

describe("build status", () => {
  it("tells every state by an icon and a word, not by colour alone", () => {
    const words = { queued: "Đang chờ", running: "Đang chạy", succeeded: "Thành công", failed: "Thất bại" } as const;
    for (const [status, word] of Object.entries(words)) {
      const { unmount } = renderVi(<BuildStatusBadge status={status as keyof typeof words} />);
      const badge = screen.getByTestId("build-status");
      expect(badge).toHaveTextContent(word);
      expect(badge).toHaveAttribute("data-status", status);
      expect(badge.querySelector("svg")).not.toBeNull();
      unmount();
    }
  });

  it("shows a failed build's error, times and no content hash", () => {
    const build: KgBuild = {
      id: 9,
      status: "failed",
      job_id: 4,
      requested_by: "alice",
      config_digest: null,
      runs: 2,
      artifact_sha256: null,
      artifact_size: null,
      artifact_reused_from: null,
      artifact_pruned_at: null,
      content_hash: null,
      nodes: null,
      edges: null,
      error: "the build reported 1 error(s): source docs is missing",
      queued_at: "2026-10-04T10:00:00Z",
      started_at: "2026-10-04T10:00:03Z",
      finished_at: "2026-10-04T10:00:13Z",
    };
    renderVi(<LatestBuild build={build} now={null} />);
    expect(screen.getByTestId("build-error")).toHaveTextContent("source docs is missing");
    expect(screen.getByText("Do alice yêu cầu")).toBeInTheDocument();
    expect(screen.getByTestId("build-wait")).toHaveTextContent("3 giây");
    expect(screen.getByTestId("build-run")).toHaveTextContent("10 giây");
    expect(screen.getByText("Chưa có: build chưa thành công")).toBeInTheDocument();
    expect(screen.getByTestId("build-artifact")).toHaveAttribute("data-state", "none");
  });

  const succeeded = (id: number, overrides: Partial<KgBuild> = {}): KgBuild => ({
    id,
    status: "succeeded",
    job_id: id,
    requested_by: null,
    config_digest: null,
    runs: 2,
    artifact_sha256: "cd".repeat(32),
    artifact_size: 20480,
    artifact_reused_from: null,
    artifact_pruned_at: null,
    content_hash: "sha256:" + "ab".repeat(32),
    nodes: 12,
    edges: 30,
    error: null,
    queued_at: "2026-10-04T10:00:00Z",
    started_at: "2026-10-04T10:00:03Z",
    finished_at: "2026-10-04T10:00:13Z",
    ...overrides,
  });

  it("says whose artifact a build points at, and when the retention deleted it", () => {
    const { unmount } = renderVi(<LatestBuild build={succeeded(12, { artifact_reused_from: 10 })} now={null} />);
    const reused = screen.getByTestId("build-artifact");
    expect(reused).toHaveAttribute("data-state", "reused");
    expect(reused).toHaveTextContent("Dùng lại tệp của build #10");
    expect(screen.getByTitle("cd".repeat(32))).toHaveTextContent("cdcdcdcdcdcd…");
    unmount();

    const pruned = succeeded(9, { artifact_sha256: null, artifact_pruned_at: "2026-10-05T03:31:00Z" });
    renderVi(<LatestBuild build={pruned} now={null} />);
    const note = screen.getByTestId("build-artifact");
    expect(note).toHaveAttribute("data-state", "pruned");
    expect(note).toHaveTextContent("Đã xoá");
    expect(note.querySelector("time")).toHaveAttribute("dateTime", "2026-10-05T03:31:00Z");
    expect(screen.getByTestId("latest-content-hash")).toHaveTextContent("sha256:" + "ab".repeat(32)); // still described
  });

  it("reads the newest build that still holds an artifact, and the history shows every artifact's state", () => {
    const builds = [
      succeeded(13, { artifact_reused_from: 11 }),
      succeeded(12, { artifact_sha256: null, artifact_pruned_at: "2026-10-05T03:31:00Z" }),
      succeeded(11),
      { ...succeeded(10), status: "failed" as const, artifact_sha256: null, error: "boom" },
    ];
    expect(latestSucceeded(builds)?.id).toBe(13);
    expect(latestSucceeded(builds.slice(1))?.id).toBe(11); // a pruned build is never the graph in use
    renderVi(<BuildHistory builds={builds} now={null} />);
    const states = screen.getAllByTestId("build-artifact").map((cell) => cell.getAttribute("data-state"));
    expect(states).toEqual(["reused", "pruned", "own", "none"]);
  });
});

describe("knowledge graph pages", () => {
  it("shows the server error with its request id and a retry on a 5xx", () => {
    withQueries(<KgOverview project="demo" query="" kind="" errors={{ builds: failure(503), graph: null, search: null }} />);
    expect(screen.getByRole("heading", { level: 1, name: "Đồ thị tri thức" })).toBeInTheDocument();
    expect(screen.getByTestId("state-error")).toHaveTextContent("rid-77");
    expect(screen.getByRole("button", { name: "Thử lại" })).toBeInTheDocument();
  });

  it("shows the no-access state on a 403 and the not-found state on a 404", () => {
    const { unmount } = withQueries(
      <KgOverview project="demo" query="" kind="" errors={{ builds: failure(403), graph: null, search: null }} />,
    );
    expect(screen.getByTestId("state-forbidden")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    unmount();
    withQueries(<KgOverview project="demo" query="" kind="" errors={{ builds: failure(404), graph: null, search: null }} />);
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Không tìm thấy dự án demo");
  });

  it("says a node it cannot show is not found, hidden and missing alike", () => {
    withQueries(<KgNodePage project="demo" id="deals:doc:acme" hops={2} errors={{ node: failure(404), neighbourhood: null }} />);
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Không tìm thấy node");
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("deals:doc:acme");
  });
});

describe("neighbourhood", () => {
  const seed = (view: Neighbourhood) => (client: ReturnType<typeof makeQueryClient>) =>
    client.setQueryData(kgKeys.neighbourhood("demo", view.focus, 2), view);

  it("shows the table alone on a narrow screen, and the table follows the selection", async () => {
    withQueries(<NeighbourhoodSection project="demo" id={VIEW.focus} hops={2} initialError={null} />, seed(VIEW));
    expect(screen.queryByTestId("kg-canvas")).toBeNull();
    expect(screen.getByText("Màn hình hẹp: đồ thị được thay bằng bảng hàng xóm.")).toBeInTheDocument();
    const table = screen.getByTestId("kg-neighbours");
    expect(table).toHaveAttribute("data-selected", VIEW.focus);
    expect(within(table).getAllByTestId("neighbour-row").map((row) => row.getAttribute("data-node-id"))).toEqual([
      "docs:doc:runbook#cài-đặt",
      "docs:doc:runbook#sổ-tay",
    ]);

    await userEvent.click(screen.getByRole("button", { name: "Chọn Cài đặt trong đồ thị" }));
    expect(screen.getByTestId("kg-neighbours")).toHaveAttribute("data-selected", "docs:doc:runbook#cài-đặt");
    expect(screen.getByRole("heading", { name: "Hàng xóm của Cài đặt" })).toBeInTheDocument();
    expect(screen.getAllByTestId("neighbour-row").map((row) => row.getAttribute("data-direction"))).toEqual(["out", "out"]);
    expect(screen.getByTestId("kg-selection-announcement")).toHaveTextContent("Đang chọn Cài đặt (Section), 2 cạnh");

    await userEvent.click(screen.getByRole("button", { name: "Về node đang xem" }));
    expect(screen.getByTestId("kg-neighbours")).toHaveAttribute("data-selected", VIEW.focus);
  });

  it("says when the graph was cut and how many nodes it left out", () => {
    const cut = { ...VIEW, truncated: true, left_out: 12, neighbours: 160 };
    withQueries(<NeighbourhoodSection project="demo" id={VIEW.focus} hops={2} initialError={null} />, seed(cut));
    const alert = screen.getByTestId("kg-truncated");
    expect(alert).toHaveTextContent("Đã cắt: đồ thị vẽ 4 node, giới hạn là 150");
    expect(alert).toHaveTextContent("Còn 12 node trong tầm với không được vẽ.");
    expect(within(alert).getByRole("link", { name: "Quan hệ theo loại cạnh" })).toHaveAttribute("href", "#kg-relations");
  });

  it("offers 1 and 2 steps as links that keep the node", () => {
    withQueries(<NeighbourhoodSection project="demo" id={VIEW.focus} hops={2} initialError={null} />, seed(VIEW));
    const hops = screen.getByTestId("kg-hops");
    expect(within(hops).getByRole("link", { name: "2 bước" })).toHaveAttribute("aria-current", "true");
    expect(within(hops).getByRole("link", { name: "1 bước" })).toHaveAttribute(
      "href",
      "/p/demo/kg/node?id=requirement%3AKB-01&hops=1",
    );
  });
});
