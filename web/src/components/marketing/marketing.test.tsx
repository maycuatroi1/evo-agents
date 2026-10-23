// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { pluginKeys } from "@/lib/plugin-queries";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { budgetSpent, budgetUsed, type MarketingChannel, type MarketingPost, type MarketingStatus, type MarketingWeekly, marketingKeys } from "./api";
import { GrowthDecisions, growthItems } from "./growth-decisions";
import { OverviewPage } from "./overview-page";
import { PostsPage } from "./posts-page";
import { WeeklyPage } from "./weekly-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/marketing/overview",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const AT = "2026-10-20T03:00:00Z";
const MONTH = { month: "2026-10", budget_usd: 50, reserved_usd: 1, spent_usd: 12 };

function channel(over: Partial<MarketingChannel>): MarketingChannel {
  return {
    id: 1,
    channel: "github",
    account: "acme/app",
    kind: "project",
    mode: "autonomous",
    adapter: "api",
    owner: null,
    target: null,
    paused: false,
    paused_reason: null,
    autonomous_since: AT,
    session_state: "none",
    account_kind: "project",
    health: "unknown",
    health_reason: null,
    health_checked_at: null,
    ...over,
  };
}

const CHANNELS = [
  channel({}),
  channel({ id: 2, channel: "x", account: "acme", paused: true, paused_reason: "Two failures in a row", health: "failing", health_reason: "no credential" }),
  channel({ id: 3, channel: "linkedin", account: "acme-page", adapter: "browser", session_state: "stale", mode: "draft_only" }),
];
const STATUS: MarketingStatus = { project: PROJECT, killed: false, reason: null, channels: 3, open_breakers: 1, budget: MONTH };

function post(id: number, state: string, over: Partial<MarketingPost> = {}): MarketingPost {
  return {
    id,
    channel_id: 1,
    channel: "github",
    account: "acme/app",
    kind: "post",
    body: `Post ${id}`,
    links: [],
    state,
    reason: null,
    layers: {},
    judge: null,
    run_id: null,
    posted_url: null,
    cost_usd: 0,
    dry_run: false,
    created_at: AT,
    posted_at: null,
    ...over,
  };
}

const WEEK: MarketingWeekly = {
  project: PROJECT,
  since: "2026-10-12T00:00:00Z",
  until: "2026-10-19T00:00:00Z",
  goals: [{ code: "G-1", statement: "People star the repo", metric: "gh.stars last over 7 days", value: 42, target: 100, reached: false }],
  posted: [{ id: 1, channel: "github", account: "acme/app", posted_url: "https://github.com/acme/app/discussions/1", posted_at: AT, cost_usd: 0.02 }],
  counts: { posted: 1, draft: 1 },
  breakers: [{ channel_id: 2, channel: "x", account: "acme", reason: "Two failures in a row", paused_at: AT }],
  killed: false,
  reason: null,
  budget: MONTH,
  week_cost_usd: 0.02,
  proposals: [{ id: 9, channel: "facebook_group", account: "bao-group", state: "draft", created_at: AT }],
  proposals_project: "team-blog",
};

function seeded(role: "reader" | "writer" | "admin", status: MarketingStatus = STATUS) {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(marketingKeys.status(PROJECT), status);
  client.setQueryData(marketingKeys.channels(PROJECT), CHANNELS);
  client.setQueryData(marketingKeys.weekly(PROJECT), WEEK);
  client.setQueryData(marketingKeys.posts(PROJECT), [
    post(1, "posted", { posted_url: "https://github.com/acme/app/discussions/1", cost_usd: 0.02 }),
    post(2, "draft", { channel: "facebook_group", account: "bao-group", body: "Chia sẻ với nhóm" }),
    post(3, "failed", { reason: "deny-list: a competitor's name" }),
    post(4, "proposed"),
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

describe("the marketing overview", () => {
  it("shows the month against its cap, each channel's breaker and session, and a writer's kill switch behind a confirmation", () => {
    withClient(<OverviewPage project={PROJECT} title="Marketing" initialError={null} />, seeded("writer"));
    expect(screen.getByTestId("marketing-budget-figures")).toHaveTextContent("13,00");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "26");
    const rows = screen.getAllByTestId("marketing-channel-row");
    expect(rows).toHaveLength(3);
    expect(within(rows[1]).getByTestId("marketing-breaker-open")).toHaveTextContent("Two failures in a row");
    expect(within(rows[2]).getByTestId("marketing-session-stale")).toBeInTheDocument();
    expect(within(rows[1]).getByTestId("marketing-health")).toHaveAttribute("data-health", "failing");
    expect(within(rows[1]).getByTestId("marketing-health")).toHaveTextContent(/Lỗi.*no credential/);
    expect(within(rows[0]).getByTestId("marketing-health")).toHaveTextContent("Chưa kiểm");
    expect(screen.queryByTestId("marketing-close-breaker")).toBeNull();
    fireEvent.click(screen.getByTestId("marketing-kill"));
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Dừng mọi bài đăng trong dự án này?");
  });

  it("gives an admin Close breaker, and a reader no control that writes", () => {
    const { unmount } = withClient(<OverviewPage project={PROJECT} title="Marketing" initialError={null} />, seeded("admin"));
    expect(screen.getAllByTestId("marketing-close-breaker")).toHaveLength(1);
    unmount();
    withClient(<OverviewPage project={PROJECT} title="Marketing" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("marketing-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("marketing-kill")).toBeNull();
    expect(screen.queryByTestId("marketing-close-breaker")).toBeNull();
  });

  it("says posting is stopped, and only an admin resumes it", () => {
    const killed = { ...STATUS, killed: true, reason: "stopped from the web" };
    const { unmount } = withClient(<OverviewPage project={PROJECT} title="Marketing" initialError={null} />, seeded("writer", killed));
    expect(screen.getByTestId("marketing-killed")).toHaveTextContent("stopped from the web");
    expect(screen.queryByTestId("marketing-unkill")).toBeNull();
    unmount();
    withClient(<OverviewPage project={PROJECT} title="Marketing" initialError={null} />, seeded("admin", killed));
    expect(screen.getByTestId("marketing-unkill")).toBeInTheDocument();
  });
});

describe("the posts page", () => {
  it("groups the posts by what the gate did, a draft with its Copy button", () => {
    withClient(<PostsPage project={PROJECT} title="Posts" initialError={null} />, seeded("writer"));
    const drafts = screen.getByTestId("marketing-group-drafts");
    expect(within(drafts).getAllByTestId("marketing-post")).toHaveLength(1);
    expect(within(drafts).getByTestId("marketing-copy")).toBeInTheDocument();
    expect(within(screen.getByTestId("marketing-group-posted")).getAllByTestId("marketing-post")).toHaveLength(1);
    expect(within(screen.getByTestId("marketing-group-stopped")).getByTestId("marketing-post-reason")).toHaveTextContent("deny-list");
    expect(within(screen.getByTestId("marketing-group-waiting")).getAllByTestId("marketing-post")).toHaveLength(1);
    expect(screen.getAllByTestId("marketing-copy")).toHaveLength(1);
  });

  it("shows a reader a note instead of the posts' text", () => {
    withClient(<PostsPage project={PROJECT} title="Posts" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("marketing-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("marketing-post")).toBeNull();
  });
});

describe("the growth weekly", () => {
  it("shows each Goal's signal against its target, the posts, the breakers, the cost and the proposals", () => {
    withClient(<WeeklyPage project={PROJECT} title="Growth weekly" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("weekly-goal")).toHaveTextContent("G-1");
    expect(screen.getByTestId("weekly-goal")).toHaveTextContent("Chưa đạt");
    expect(screen.getAllByTestId("weekly-post")).toHaveLength(1);
    expect(screen.getByTestId("weekly-breaker")).toHaveTextContent("Two failures in a row");
    expect(screen.getByTestId("weekly-proposals")).toHaveTextContent("team-blog");
    expect(screen.getByTestId("weekly-proposal")).toHaveTextContent("facebook_group");
  });
});

describe("the growth decisions", () => {
  it("lists an open breaker, an expired session and a spent budget", () => {
    const words = { breaker: (c: string, a: string) => `breaker ${c} ${a}`, session: (c: string, a: string) => `session ${c} ${a}`, budget: (m: string) => `budget ${m}` };
    expect(growthItems(CHANNELS, STATUS, words).map((item) => item.kind)).toEqual(["breaker", "session"]);
    const spent = { ...STATUS, budget: { ...MONTH, spent_usd: 49.5, reserved_usd: 0.5 } };
    expect(growthItems([], spent, words)).toEqual([{ kind: "budget", key: "budget", title: "budget 2026-10" }]);
    expect(budgetSpent(spent.budget)).toBe(true);
    expect(budgetUsed(MONTH)).toBe(26);
  });

  it("shows nothing in a project without the plugin, and the items once it is on", () => {
    const client = seeded("writer");
    client.setQueryData(pluginKeys.projectList(PROJECT), []);
    const { unmount } = withClient(<GrowthDecisions project={PROJECT} />, client);
    expect(screen.queryByTestId("decide-growth")).toBeNull();
    unmount();
    const on = seeded("writer");
    on.setQueryData(pluginKeys.projectList(PROJECT), [{ name: "marketing", enabled: true, state: "installed" }]);
    withClient(<GrowthDecisions project={PROJECT} />, on);
    const items = screen.getAllByTestId("growth-item");
    expect(items.map((item) => item.dataset.kind)).toEqual(["breaker", "session"]);
    expect(within(items[0]).getByTestId("growth-item-open")).toHaveAttribute("href", "/p/demo/x/marketing/overview");
  });
});
