// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { loopKeys } from "@/lib/loop-queries";
import { type OpportunityEvidence, opportunityKeys, type ProjectOpportunity, type ProjectOpportunityShown } from "@/lib/opportunity-queries";
import { queryKeys } from "@/lib/queries";
import { waitingKeys } from "@/lib/waiting-proposals";
import { renderVi } from "@/test/render";

import { OpportunitiesPage } from "./opportunities-page";
import { OpportunityPage } from "./opportunity-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/northwind/opportunities",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "northwind";

function opportunity(number: number, values: Partial<ProjectOpportunity> = {}): ProjectOpportunity {
  return {
    project: PROJECT,
    code: `O-${number}`,
    number,
    revision: 1,
    statement: `A pilot, case ${number}`,
    beneficiary: "Shop owners",
    leverage: "An intro from the fair",
    window_until: null,
    window_note: null,
    answers: [],
    hidden_answers: 0,
    metric: null,
    rank: number,
    owner: "mai",
    status: "open",
    close_reason: null,
    note: null,
    created_by: "mai",
    created_at: "2026-10-20T08:00:00Z",
    ...values,
  };
}

const SHOWN: ProjectOpportunityShown = {
  ...opportunity(1, { revision: 2, window_until: "2099-12-01", window_note: "the fair", answers: ["P-1", "lumen/P-3"], hidden_answers: 2 }),
  evidence_count: 2,
  source_count: 2,
};

function line(number: number, signal: string, excerpt: string, confirmed: boolean): OpportunityEvidence {
  return {
    project: PROJECT,
    opportunity: "O-1",
    code: `E-${number}`,
    number,
    source_kind: "messenger",
    source_uri: `messenger://group/${number}`,
    locator: null,
    speaker: "Lan",
    excerpt,
    signal,
    label: "public",
    added_by: "mai",
    added_at: "2026-10-20T08:00:00Z",
    confirmed_by: confirmed ? "bao" : null,
    confirmed_at: confirmed ? "2026-10-21T08:00:00Z" : null,
    proposal_id: null,
  };
}

function seeded(role: "reader" | "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(opportunityKeys.list(PROJECT), [
    opportunity(1),
    opportunity(2, { window_until: "2099-12-01" }),
    opportunity(3, { window_until: "2099-11-01", status: "missed" }),
    opportunity(4, { window_until: "2099-11-15", answers: ["P-1"], hidden_answers: 1 }),
  ]);
  client.setQueryData(opportunityKeys.one(PROJECT, "O-1"), SHOWN);
  client.setQueryData(opportunityKeys.revisions(PROJECT, "O-1"), [
    { revision: 2, status: "open", note: null, created_by: "bao", created_at: "2026-10-22T08:00:00Z", changed: ["window_until"] },
    { revision: 1, status: "open", note: "from the fair", created_by: "mai", created_at: "2026-10-20T08:00:00Z", changed: ["statement"] },
  ]);
  client.setQueryData(opportunityKeys.evidence(PROJECT, "O-1"), [line(1, "wish", "We wish we could sell to the chain", true), line(2, "complaint", "Orders take two minutes", false)]);
  client.setQueryData(loopKeys.goalList(PROJECT), [
    { code: "G-1", statement: "Win the chain", status: "active", opportunity: "O-1", close_reason: null },
    { code: "G-2", statement: "Another", status: "active", opportunity: null, close_reason: null },
  ]);
  client.setQueryData(loopKeys.items(PROJECT, { state: null, kind: null, goal: null, open: false, opportunity: "O-1" }), { items: [], more: false });
  client.setQueryData(waitingKeys.of(PROJECT, "opportunity"), []);
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

describe("the opportunities list", () => {
  it("lists the nearest window first and those with no date last, and filters by status", async () => {
    withClient(<OpportunitiesPage project={PROJECT} initialError={null} />, seeded("writer"));
    const table = screen.getByTestId("opportunities-table");
    expect(within(table).getAllByTestId("opportunity-link").map((link) => link.getAttribute("data-opportunity"))).toEqual(["O-3", "O-4", "O-2", "O-1"]);
    expect(within(table).getAllByTestId("opportunity-status").map((pill) => pill.textContent)).toEqual(["Đã lỡ", "Đang mở", "Đang mở", "Đang mở"]);
    expect(screen.getByTestId("opportunities-new")).toHaveTextContent("Cơ hội mới");
    await userEvent.click(within(screen.getByTestId("opportunity-statuses")).getByRole("button", { name: /Đã lỡ/ }));
    expect(within(screen.getByTestId("opportunities-table")).getAllByTestId("opportunity-link").map((link) => link.getAttribute("data-opportunity"))).toEqual(["O-3"]);
  });

  it("gives a reader no way to write", () => {
    withClient(<OpportunitiesPage project={PROJECT} initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("opportunities-table")).toBeInTheDocument();
    expect(screen.queryByTestId("opportunities-new")).toBeNull();
  });
});

describe("an opportunity's page", () => {
  it("shows the fields, the evidence by signal, the answers with a count of the hidden ones and the goals that answer it", () => {
    withClient(<OpportunityPage project={PROJECT} code="O-1" initialError={null} />, seeded("writer"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("A pilot, case 1");
    expect(screen.getByTestId("opportunity-beneficiary")).toHaveTextContent("Shop owners");
    expect(screen.getByTestId("opportunity-leverage")).toHaveTextContent("An intro from the fair");
    expect(screen.getByTestId("opportunity-window")).toHaveTextContent("the fair");
    expect(screen.getByTestId("opportunity-days-left")).toHaveTextContent("Còn");
    expect(screen.getAllByTestId("opportunity-signal").map((group) => group.getAttribute("data-signal"))).toEqual(["complaint", "wish"]);
    expect(screen.getAllByTestId("opportunity-evidence-state").map((state) => state.textContent)).toEqual(["chưa xác nhận", "bao đã xác nhận"]);
    const answers = screen.getByTestId("opportunity-answers-panel");
    expect(within(answers).getByText("P-1")).toBeInTheDocument();
    expect(within(answers).getByText("lumen/P-3")).toBeInTheDocument();
    expect(within(answers).getByTestId("opportunity-hidden-answers")).toHaveTextContent("+2");
    expect(within(screen.getByTestId("opportunity-goals")).getAllByTestId("opportunity-goal").map((row) => row.getAttribute("data-goal"))).toEqual(["G-1"]);
    expect(screen.getAllByTestId("opportunity-revision-row").map((row) => row.getAttribute("data-revision"))).toEqual(["2", "1"]);
    expect(screen.getByTestId("opportunity-close")).toBeInTheDocument();
  });

  it("tells a reader they only read, with no button that writes", () => {
    withClient(<OpportunityPage project={PROJECT} code="O-1" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("opportunity-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("opportunity-edit")).toBeNull();
    expect(screen.queryByTestId("opportunity-close")).toBeNull();
  });
});
