// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiErrorInfo } from "@/lib/api/errors";
import { kgKeys, NODE_PAGE_GROUPING } from "@/lib/kg/queries";
import type { GraphNode, GraphShape, KgBuild, KgBuilds, Neighbourhood, SourceRead } from "@/lib/kg/types";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { BuildStatusBadge } from "./badges";
import { BuildHistory, LatestBuild, latestSucceeded } from "./build-status";
import { GraphShapeSection } from "./graph-shape";
import { KgNodePage } from "./kg-node-page";
import { KgOverview } from "./kg-overview";
import { NeighbourhoodSection } from "./neighbourhood";
import { SourceCommits } from "./source-commits";
import { StatusTiles } from "./status-tiles";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/p/demo/kg",
  useSearchParams: () => new URLSearchParams(),
}));

const label = { level: "internal", location: "any", integrity: "U" };
const node = (id: string, hop: number, name: string, kind = "Section", degree = 1): GraphNode => ({
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
  degree,
});

const VIEW: Neighbourhood = {
  graph: { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 4, edges: 3, finished_at: "2026-10-04T10:00:00Z", latest: true },
  focus: "requirement:KB-01",
  hops: 2,
  limit: 150,
  group: 5,
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
  groups: [
    {
      key: "in:mentions:Section",
      rel: "mentions",
      direction: "in",
      kind: "Section",
      total: 2,
      shown: ["docs:doc:runbook#cài-đặt", "docs:doc:runbook#sổ-tay"],
    },
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

describe("commits read", () => {
  const sha = "4f1c2e7a9b3d5f60718293a4b5c6d7e8f9012345";
  const read = (source: string, overrides: Partial<SourceRead> = {}): SourceRead => ({
    source,
    commit: sha,
    ref: "origin/main",
    base: "origin/main",
    behind: 0,
    url: `https://github.com/acme/${source}/commit/${sha}`,
    ...overrides,
  });

  it("names each source's commit and marks only a source behind its remote's default branch", () => {
    renderVi(
      <SourceCommits
        sources={[
          read("app", { ref: "HEAD", behind: 254 }),
          read("docs"),
          read("deals", { base: null, behind: null, url: null, ref: null }),
        ]}
      />,
    );
    const rows = screen.getAllByTestId("source-commit");
    expect(rows.map((row) => row.getAttribute("data-source"))).toEqual(["app", "docs", "deals"]);
    const [app, docs, deals] = rows;
    expect(within(app).getByTestId("source-commit-sha")).toHaveTextContent("4f1c2e7");
    expect(within(app).getByRole("link", { name: "Mở commit 4f1c2e7 (mở trong tab mới)" })).toHaveAttribute(
      "href",
      `https://github.com/acme/app/commit/${sha}`,
    );
    expect(within(app).getByText("của HEAD")).toBeInTheDocument();
    expect(within(app).getByTestId("source-behind")).toHaveTextContent("chậm origin/main 254 commit");
    expect(within(app).getByTestId("source-behind")).toHaveAttribute("data-behind", "254");
    expect(within(docs).getByTestId("source-behind")).toHaveTextContent("khớp với origin/main");
    expect(within(deals).queryByRole("link")).toBeNull();
    expect(within(deals).getByText("của HEAD")).toBeInTheDocument();
    expect(within(deals).getByText("không có nhánh remote để so")).toBeInTheDocument();
  });

  it("shows nothing when no source read a commit", () => {
    renderVi(<SourceCommits sources={[]} />);
    expect(screen.queryByTestId("kg-commits")).toBeNull();
  });
});

describe("neighbourhood", () => {
  const seed = (view: Neighbourhood) => (client: ReturnType<typeof makeQueryClient>) =>
    client.setQueryData(kgKeys.neighbourhood("demo", view.focus, 2, NODE_PAGE_GROUPING), view);

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

  it("shows the most connected of each group first, says how many it holds back, and opens the group on a click", async () => {
    const hub = (i: number) => `docs:doc:hub#${i}`;
    const many: Neighbourhood = {
      ...VIEW,
      nodes: [
        ...VIEW.nodes,
        ...[1, 2, 3, 4, 5].map((i) => node(hub(i), 1, `Mục ${i}`, "Section", 10 - i)),
      ],
      edges: [
        ...VIEW.edges,
        ...[1, 2, 3, 4, 5].map((i) => ({ id: `h${i}`, src: VIEW.focus, rel: "defines", dst: hub(i), status: "parsed" })),
      ],
      groups: [
        { key: "out:defines:Section", rel: "defines", direction: "out", kind: "Section", total: 8, shown: [1, 2, 3, 4, 5].map(hub) },
        ...VIEW.groups,
      ],
      neighbours: 10,
    };
    const opened: Neighbourhood = {
      ...many,
      nodes: [...many.nodes, ...[6, 7, 8].map((i) => node(hub(i), 1, `Mục ${i}`, "Section", 10 - i))],
      edges: [
        ...many.edges,
        ...[6, 7, 8].map((i) => ({ id: `h${i}`, src: VIEW.focus, rel: "defines", dst: hub(i), status: "parsed" })),
      ],
      groups: [{ ...many.groups[0], shown: [1, 2, 3, 4, 5, 6, 7, 8].map(hub) }, ...VIEW.groups],
    };
    withQueries(<NeighbourhoodSection project="demo" id={VIEW.focus} hops={2} initialError={null} />, (client) => {
      client.setQueryData(kgKeys.neighbourhood("demo", VIEW.focus, 2, NODE_PAGE_GROUPING), many);
      client.setQueryData(
        kgKeys.neighbourhood("demo", VIEW.focus, 2, { ...NODE_PAGE_GROUPING, open: ["out:defines:Section"] }),
        opened,
      );
    });
    const groups = screen.getAllByTestId("neighbour-group");
    expect(groups.map((group) => [group.getAttribute("data-key"), group.getAttribute("data-hidden")])).toEqual([
      ["out:defines:Section", "3"],
      ["in:mentions:Section", "0"],
    ]);
    expect(within(groups[0]).getByText("5 trên 8")).toBeInTheDocument();
    expect(within(groups[0]).getAllByTestId("neighbour-row").map((row) => row.getAttribute("data-node-id"))).toEqual(
      [1, 2, 3, 4, 5].map(hub),
    );
    expect(within(groups[1]).queryByTestId("neighbour-group-more")).toBeNull();

    await userEvent.click(within(groups[0]).getByRole("button", { name: "Hiện thêm 3 (defines, Section)" }));
    const whole = screen.getAllByTestId("neighbour-group")[0];
    expect(whole).toHaveAttribute("data-hidden", "0");
    expect(within(whole).getAllByTestId("neighbour-row")).toHaveLength(8);
    expect(within(whole).getByText("8 trên 8")).toBeInTheDocument();
  });
});

describe("knowledge graph overview", () => {
  const hidden: KgBuild = {
    id: 21,
    status: "succeeded",
    job_id: 3,
    requested_by: null,
    config_digest: null,
    runs: 1,
    artifact_sha256: "b".repeat(64),
    artifact_size: 10,
    artifact_reused_from: null,
    artifact_pruned_at: null,
    content_hash: "sha256:" + "c".repeat(64),
    nodes: null,
    edges: null,
    error: null,
    queued_at: "2026-10-04T10:00:00Z",
    started_at: "2026-10-04T10:00:03Z",
    finished_at: "2026-10-04T10:01:00Z",
  };
  const data = (build: KgBuild): KgBuilds => ({ builds: [build], jobs: [] });

  it("sizes a graph that hides its totals by what the reader can read, and the build's totals otherwise", () => {
    const visible = { ...VIEW.graph, build_id: 21, nodes: 42, edges: 17 };
    const { unmount } = renderVi(<StatusTiles data={data(hidden)} now={null} visible={visible} />);
    const tile = screen.getByTestId("tile-size");
    expect(tile).toHaveTextContent("42 node");
    expect(tile).toHaveTextContent("17 cạnh, trong phần bạn đọc được");
    unmount();

    const { unmount: again } = renderVi(<StatusTiles data={data(hidden)} now={null} visible={null} />);
    expect(screen.getByTestId("tile-size")).toHaveTextContent("-");
    again();

    renderVi(<StatusTiles data={data({ ...hidden, nodes: 900, edges: 1200 })} now={null} visible={visible} />);
    expect(screen.getByTestId("tile-size")).toHaveTextContent("900 node");
    expect(screen.getByTestId("tile-size")).toHaveTextContent("1.200 cạnh");
    expect(screen.getByTestId("tile-size")).not.toHaveTextContent("bạn đọc được");
  });

  it("lists the graph's shape, the most used kinds and edge type first", () => {
    const shape: GraphShape = {
      graph: VIEW.graph,
      shape: [
        { src_kind: "Section", rel: "part_of", dst_kind: "Document", count: 3 },
        { src_kind: "Section", rel: "mentions", dst_kind: "Requirement", count: 11 },
      ],
    };
    withQueries(<GraphShapeSection project="demo" />, (client) => client.setQueryData(kgKeys.shape("demo"), shape));
    expect(screen.getByRole("heading", { name: "Hình dạng đồ thị" })).toBeInTheDocument();
    const table = screen.getByTestId("kg-shape-table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell").map((cell) => cell.textContent))).toEqual([
      ["Section", "mentions", "Requirement", "11"],
      ["Section", "part_of", "Document", "3"],
    ]);
  });

  it("says when the reader can read no edge, and when there is no graph", () => {
    const { unmount } = withQueries(<GraphShapeSection project="demo" />, (client) =>
      client.setQueryData(kgKeys.shape("demo"), { graph: VIEW.graph, shape: [] }),
    );
    expect(screen.getByTestId("kg-shape-empty")).toBeInTheDocument();
    unmount();
    withQueries(<GraphShapeSection project="demo" />, (client) =>
      client.setQueryData(kgKeys.shape("demo"), { graph: null, shape: [] }),
    );
    expect(screen.queryByTestId("kg-shape-table")).toBeNull();
  });
});
