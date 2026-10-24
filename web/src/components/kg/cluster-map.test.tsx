// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ApiErrorInfo } from "@/lib/api/errors";
import { kgKeys } from "@/lib/kg/queries";
import type { ClusterDetail, ClusterMap, ClusterSummary, NodeSearch } from "@/lib/kg/types";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { ClusterMapSection } from "./cluster-map";
import { ClusterMatrix, ClusterTable } from "./cluster-table";
import { KgClusterPage } from "./kg-cluster-page";
import { SearchResults } from "./node-search";

const params = { current: new URLSearchParams() };

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/p/demo/kg",
  useSearchParams: () => params.current,
}));

const label = { level: "internal", location: "any", integrity: "U" };
const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 5, edges: 4, finished_at: "2026-10-04T10:00:00Z", latest: true };

const summary = (id: string, name: string, size: number, extra: Partial<ClusterSummary> = {}): ClusterSummary => ({
  id,
  level: 1,
  parent: null,
  label: name,
  size,
  kinds: { Section: size },
  subclusters: 0,
  ...extra,
});

const MAP: ClusterMap = {
  graph,
  computed: true,
  reason: null,
  clusters: [summary("c0", "runbook, cài đặt", 3, { subclusters: 2 }), summary("c1", "billing", 2)],
  links: [{ a: "c0", b: "c1", weight: 3 }],
  nodes: [
    { id: "docs:doc:runbook", kind: "Document", name: "runbook", cluster: "c0", sub: "c0.0" },
    { id: "requirement:KB-01", kind: "Requirement", name: "KB-01", cluster: "c0", sub: "c0.1" },
    { id: "app:main.py", kind: "File", name: "main.py", cluster: "c0", sub: "c0.1" },
    { id: "app:billing.py", kind: "File", name: "billing.py", cluster: "c1", sub: null },
    { id: "app:invoice.py", kind: "File", name: "invoice.py", cluster: "c1", sub: null },
  ],
  edges: [
    [0, 1, 1],
    [2, 3, 3],
  ],
};

const DETAIL: ClusterDetail = {
  graph,
  cluster: MAP.clusters[0]!,
  subclusters: [summary("c0.0", "runbook", 1, { level: 2, parent: "c0" }), summary("c0.1", "KB-01, main", 2, { level: 2, parent: "c0" })],
  members: MAP.nodes.slice(0, 3),
  edges: [[0, 1, 1]],
  neighbours: [{ id: "c1", label: "billing", size: 2, weight: 3 }],
};

function withQueries(ui: ReactElement, seed: (client: ReturnType<typeof makeQueryClient>) => void = () => {}) {
  const client = makeQueryClient();
  seed(client);
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const notFound: ApiErrorInfo = { status: 404, code: "not_found", message: "from the API", requestId: "rid-1" };

beforeEach(() => {
  params.current = new URLSearchParams();
});

describe("cluster map", () => {
  it("lists the clusters, largest first, each linking to its page, with the matrix beside them", () => {
    renderVi(
      <>
        <ClusterTable project="demo" clusters={MAP.clusters} focus={1} />
        <ClusterMatrix clusters={MAP.clusters} links={MAP.links} />
      </>,
    );
    const links = screen.getAllByTestId("kg-cluster-link");
    expect(links.map((link) => link.textContent)).toEqual(["runbook, cài đặt", "billing"]);
    expect(links[0]).toHaveAttribute("href", "/p/demo/kg/cluster?id=c0");
    expect(links[1]).toHaveAttribute("aria-current", "true");
    expect(screen.getByTestId("kg-cluster-table")).toHaveTextContent("2 cụm, lớn nhất trước");
    const cells = within(screen.getByTestId("kg-cluster-matrix")).getAllByTestId("kg-matrix-cell");
    expect(cells).toHaveLength(2);
    expect(cells[0]).toHaveTextContent("runbook, cài đặt và billing: 3 cạnh");
  });

  it("says the matrix is empty when no edge joins two clusters", () => {
    renderVi(<ClusterMatrix clusters={MAP.clusters} links={[]} />);
    expect(screen.getByTestId("kg-matrix-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("kg-cluster-matrix")).toBeNull();
  });

  it("falls back to the list and the matrix, with a line saying why, without WebGL2", async () => {
    withQueries(<ClusterMapSection project="demo" />, (client) => client.setQueryData(kgKeys.clusters("demo"), MAP));
    expect(screen.getByRole("heading", { name: "Bản đồ cụm" })).toBeInTheDocument();
    expect(screen.getByTestId("kg-map-summary")).toHaveTextContent("2 cụm, 5 node, 1 liên kết giữa các cụm");
    expect(await screen.findByTestId("kg-map-no-webgl")).toHaveTextContent("WebGL2");
    expect(screen.queryByTestId("kg-map-mode")).toBeNull();
    expect(screen.getByTestId("kg-cluster-table")).toBeInTheDocument();
    expect(screen.getByTestId("kg-cluster-matrix")).toBeInTheDocument();
  });

  it("lights up the cluster of the node a search result points at, and clears it", async () => {
    params.current = new URLSearchParams({ focus: "app:billing.py" });
    withQueries(<ClusterMapSection project="demo" />, (client) => client.setQueryData(kgKeys.clusters("demo"), MAP));
    expect(screen.getByTestId("kg-map-focus")).toHaveTextContent("cụm của app:billing.py");
    expect(screen.getAllByTestId("kg-cluster-link")[1]).toHaveAttribute("aria-current", "true");
    await userEvent.click(within(screen.getByTestId("kg-map-focus")).getByRole("button", { name: "Bỏ" }));
    expect(screen.queryByTestId("kg-map-focus")).toBeNull();
    expect(screen.getAllByTestId("kg-cluster-link")[1]).not.toHaveAttribute("aria-current");
  });

  it("gives the reason when there are no clusters", () => {
    withQueries(<ClusterMapSection project="demo" />, (client) =>
      client.setQueryData(kgKeys.clusters("demo"), { ...MAP, computed: false, reason: "built before clusters", clusters: [], links: [], nodes: [], edges: [] }),
    );
    expect(screen.getByTestId("kg-map-empty")).toHaveTextContent("Không có cụm để hiện: built before clusters");
    expect(screen.queryByTestId("kg-cluster-table")).toBeNull();
  });

  it("links each search result to its place on the map", () => {
    const search: NodeSearch = {
      graph,
      query: "main",
      kinds: [],
      results: [
        { id: "app:main.py::run", kind: "Symbol", name: "run", status: "parsed", conf: 1, label, props: { file: "app:main.py" }, aliases: [] },
      ],
    };
    withQueries(<SearchResults project="demo" query="main" kind="" initialError={null} />, (client) =>
      client.setQueryData(kgKeys.search("demo", "main", ""), search),
    );
    expect(screen.getByTestId("kg-result-map")).toHaveAttribute("href", "/p/demo/kg?q=main&focus=app%3Amain.py#kg-map");
  });
});

describe("cluster page", () => {
  it("shows the cluster, its parts, its neighbours and its members, which a filter narrows", async () => {
    withQueries(<KgClusterPage project="demo" id="c0" error={null} />, (client) =>
      client.setQueryData(kgKeys.cluster("demo", "c0"), DETAIL),
    );
    expect(screen.getByTestId("kg-cluster-title")).toHaveTextContent("runbook, cài đặt");
    expect(within(screen.getByTestId("kg-subcluster-table")).getAllByTestId("kg-cluster-link")).toHaveLength(2);
    const neighbour = within(screen.getByTestId("kg-neighbour-clusters")).getByRole("link", { name: "billing" });
    expect(neighbour).toHaveAttribute("href", "/p/demo/kg/cluster?id=c1");
    expect(screen.getAllByTestId("kg-member-link")).toHaveLength(3);
    await userEvent.type(screen.getByTestId("kg-member-filter"), "FILE");
    expect(screen.getAllByTestId("kg-member-link").map((link) => link.textContent)).toEqual(["main.py"]);
    expect(screen.getByTestId("kg-member-table")).toHaveTextContent("1 trên 3 thành viên");
  });

  it("says a cluster it cannot show is not found, and asks for one without an id", () => {
    const { unmount } = withQueries(<KgClusterPage project="demo" id="c9" error={notFound} />);
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Không có cụm nào với id đó");
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("c9");
    unmount();
    withQueries(<KgClusterPage project="demo" id="" error={null} />);
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Chưa chọn cụm");
  });
});
