// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { type CanvasBaselineItem, type CanvasClaim, type CanvasDetail, canvasKeys } from "./api";
import { BaselineItem } from "./baseline-item";
import { CanvasPage } from "./canvas-page";
import { CELLS } from "./model";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/canvas/canvases/northwind",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const SLUG = "northwind";

function claimOf(overrides: Partial<CanvasClaim> = {}): CanvasClaim {
  return {
    code: "CL-1",
    number: 1,
    cell: "problem",
    position: 1,
    text: "Shop owners lose a sale when checkout takes more than two minutes",
    status: "testing",
    confidence: "medium",
    test_card: {
      hypothesis: "Owners pay for one-click checkout",
      test: "Offer it to 20 shops",
      metric: "checkout.conversion",
      aggregate: "mean",
      window_days: 30,
      comparator: ">=",
      threshold: 0.4,
      min_samples: null,
      tenant: null,
      deadline: "2020-01-31",
    },
    archived: false,
    use_cases: [],
    signal_keys: ["checkout.conversion"],
    rev: 2,
    evidence_count: 1,
    real_support_count: 0,
    label: {},
    created_by: "mai",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-02T00:00:00Z",
    archived_at: null,
    ...overrides,
  };
}

function canvasOf(claims: CanvasClaim[]): CanvasDetail {
  return {
    slug: SLUG,
    name: "Northwind Checkout",
    description: "",
    label: {},
    created_by: "mai",
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-02T00:00:00Z",
    archived_at: null,
    claims: claims.length,
    statuses: { testing: 1, assumption: 1 },
    cells: CELLS.map((cell) => ({ cell, claims: claims.filter((claim) => claim.cell === cell) })),
  };
}

function seeded(role: "reader" | "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  const claims = [claimOf(), claimOf({ code: "CL-2", number: 2, cell: "channels", status: "assumption", test_card: null, evidence_count: 0 })];
  client.setQueryData(canvasKeys.detail(PROJECT, SLUG, false), canvasOf(claims));
  client.setQueryData(canvasKeys.proposals(PROJECT, SLUG), []);
  client.setQueryData(canvasKeys.baselines(PROJECT, SLUG), [
    { number: 3, kind: "push", week: null, due_at: null, time_zone: null, status: "proposed", created_by: "mai", created_at: "2026-10-02T00:00:00Z", approved_at: null, expired_at: null, item_count: 1, waiting: 1 },
  ]);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return client;
}

function withClient(ui: ReactElement, client: QueryClient) {
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("the canvas page", () => {
  it("draws the nine cells in Lean Canvas order, each claim in its cell with its status, and what waits for review", () => {
    withClient(<CanvasPage project={PROJECT} slug={SLUG} archived={false} initialError={null} />, seeded("writer"));
    const cells = screen.getAllByTestId("canvas-cell");
    expect(cells.map((cell) => cell.dataset.cell)).toEqual([...CELLS]);
    const problem = cells[0];
    const card = within(problem).getByTestId("claim-card");
    expect(card.dataset.claim).toBe("CL-1");
    expect(within(card).getByTestId("claim-status")).toHaveTextContent("Đang kiểm");
    expect(within(card).getByTestId("claim-deadline")).toHaveAttribute("data-overdue", "true");
    expect(within(card).getByTestId("claim-card-text")).toHaveAttribute("href", "/p/demo/x/canvas/canvases/northwind/claims/CL-1");
    expect(within(cells[4]).getByTestId("claim-card").dataset.claim).toBe("CL-2");
    expect(screen.getByTestId("canvas-baseline-waiting")).toBeInTheDocument();
    expect(screen.getAllByTestId("cell-add")).toHaveLength(9);
    expect(screen.queryByTestId("canvas-read-only")).toBeNull();
  });

  it("gives a reader no control that writes", () => {
    withClient(<CanvasPage project={PROJECT} slug={SLUG} archived={false} initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("canvas-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("cell-add")).toBeNull();
    expect(screen.queryByTestId("claim-new")).toBeNull();
  });
});

const ITEM: CanvasBaselineItem = {
  claim: "CL-1",
  kind: "overdue",
  from_status: "testing",
  at_status: "testing",
  to_status: null,
  claim_rev: 2,
  proposal_id: null,
  deadline: "2020-01-31",
  evidence: [],
  hidden_evidence: 0,
  proposers: ["mai"],
  decision: null,
  decided_by: null,
  decided_at: null,
  new_deadline: null,
  reason: null,
  rev: null,
  decisions: ["extended", "retired", "deferred"],
};

describe("an item of a baseline", () => {
  it("offers a reviewer to extend with a new deadline, retire or defer an overdue test card", () => {
    withClient(<BaselineItem project={PROJECT} slug={SLUG} number={3} item={ITEM} open viewer="bao" />, seeded("writer"));
    expect(screen.getByTestId("baseline-deadline")).toBeInTheDocument();
    expect(screen.getByTestId("baseline-extended")).toBeInTheDocument();
    expect(screen.getByTestId("baseline-retired")).toBeInTheDocument();
    expect(screen.getByTestId("baseline-deferred")).toBeInTheDocument();
  });

  it("tells whoever changed the status that another writer decides it", () => {
    withClient(<BaselineItem project={PROJECT} slug={SLUG} number={3} item={{ ...ITEM, decisions: [] }} open viewer="mai" />, seeded("writer"));
    expect(screen.queryByTestId("baseline-decide")).toBeNull();
    expect(screen.getByTestId("baseline-not-yours")).toHaveTextContent("Bạn đã đổi trạng thái");
  });

  it("shows the decision once made, and no form", () => {
    const decided = { ...ITEM, decision: "extended" as const, decided_by: "bao", decided_at: "2026-10-03T00:00:00Z", new_deadline: "2026-12-31", decisions: [] };
    withClient(<BaselineItem project={PROJECT} slug={SLUG} number={3} item={decided} open={false} viewer="bao" />, seeded("writer"));
    expect(screen.getByTestId("baseline-decided")).toHaveAttribute("data-decision", "extended");
    expect(screen.queryByTestId("baseline-decide")).toBeNull();
  });
});
