// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { PlanSummary } from "@/lib/plans";
import { renderVi } from "@/test/render";

import { CompletePlanButton } from "./complete-plan";

const api = vi.hoisted(() => ({ posts: [] as { path: string; body: unknown }[], status: 200 }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (request.method === "POST") {
          api.posts.push({ path: url.pathname, body: await request.json() });
          if (api.status !== 200) return Response.json({ error: "conflict", message: "busy" }, { status: api.status });
          return Response.json({ plan_id: "agent-hub", area: "completed", revision: 5, changed: true });
        }
        return Response.json({ error: "not_found", message: url.pathname }, { status: 404 });
      },
    }),
}));

const PLAN: PlanSummary = {
  plan_id: "agent-hub",
  area: "active",
  revision: 4,
  digest: "d",
  title: "Hub cho agent",
  steps_total: 30,
  steps_done: 12,
  updated_at: "2026-10-04T02:00:00Z",
  updated_by: "alice",
};

function renderButton(plan: PlanSummary = PLAN) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderVi(
    <QueryClientProvider client={client}>
      <CompletePlanButton project="demo" plan={plan} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.posts = [];
  api.status = 200;
});

describe("CompletePlanButton", () => {
  it("says how many open steps it marks done, then completes the plan at its revision", async () => {
    renderButton();
    await userEvent.click(screen.getByTestId("plans-complete"));
    expect(screen.getByTestId("plans-complete-confirm")).toHaveTextContent("18 bước chưa xong sẽ được đánh dấu done");
    await userEvent.click(screen.getByTestId("plans-complete-confirm-confirm"));
    await waitFor(() => expect(screen.queryByTestId("plans-complete-confirm")).not.toBeInTheDocument());
    expect(api.posts).toEqual([
      { path: "/v1/projects/demo/plans/agent-hub/complete", body: { if_revision: 4, mark_steps_done: true } },
    ]);
  });

  it("keeps the confirmation open with the refusal on a 409", async () => {
    api.status = 409;
    renderButton({ ...PLAN, steps_done: 30 });
    await userEvent.click(screen.getByTestId("plans-complete"));
    expect(screen.getByTestId("plans-complete-confirm")).toHaveTextContent("Plan chuyển sang mục Đã hoàn tất.");
    await userEvent.click(screen.getByTestId("plans-complete-confirm-confirm"));
    expect(await screen.findByText("Plan vừa đổi trên hub. Tải lại trang rồi thử lại.")).toBeInTheDocument();
  });
});
