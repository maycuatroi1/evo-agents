// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { toast as sonner } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import type { Run } from "./queries";
import { AsOwnerBadge, RunAccess } from "./run-access";
import { RunCredentials } from "./run-credentials";

/** A run that runs as its owner: the card that says so, who let it and since when, and the owner's Revoke. */

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { url: string; method: string; headers: Headers; body: string }[],
  leases: [] as unknown[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push({ url: request.url, method: request.method, headers: request.headers, body: await request.clone().text() });
        const path = new URL(request.url).pathname;
        if (path === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (path === "/v1/projects/demo/runs/12/credentials") return Response.json(api.leases);
        if (api.answer) return api.answer(request);
        return Response.json({ error: "not_found", message: path }, { status: 404 });
      },
    }),
}));

const RUN = {
  id: 12,
  project: "demo",
  kind: "plan",
  state: "running",
  plan_id: "rollout",
  step_key: null,
  dispatched_by: "octo",
  queued_at: "2026-10-27T03:00:00Z",
  as_owner: true,
  as_owner_since: "2026-10-27T03:00:00Z",
} as unknown as Run;

function show(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  return { user: userEvent.setup(), client, view: renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
  api.leases = [];
  sonner.dismiss();
});

describe("RunAccess", () => {
  it("tells the owner the run acts as them, who let it and since when, and offers Revoke", () => {
    show(<RunAccess run={RUN} owner />);
    const card = screen.getByRole("region", { name: "Quyền trên hub" });
    expect(within(card).getByTestId("run-as-owner")).toHaveTextContent("Chạy như octo");
    expect(within(card).getByTestId("run-access-text")).toHaveTextContent("không bao giờ chạm tới secrets, tokens hay workers của bạn");
    expect(within(card).getByTestId("run-access-by")).toHaveTextContent("octo");
    expect(within(card).getByTestId("run-access-since").querySelector("time")).toHaveAttribute("dateTime", "2026-10-27T03:00:00Z");
    expect(within(card).getByTestId("run-access-ends")).toHaveTextContent("khi run dừng, hoặc khi bạn thu hồi");
    expect(within(card).getByRole("button", { name: "Thu hồi quyền trên hub của run #12" })).toBeInTheDocument();
  });

  it("shows anyone else that the run acts as its owner, but no Revoke", () => {
    show(<RunAccess run={RUN} owner={false} />);
    const card = screen.getByTestId("run-access");
    expect(within(card).getByTestId("run-access-text")).toHaveTextContent("Agent gọi API của hub dưới tên octo");
    expect(within(card).getByTestId("run-access-ends")).toHaveTextContent("khi run dừng, hoặc khi octo thu hồi");
    expect(within(card).queryByTestId("run-access-revoke")).toBeNull();
  });

  it("says a run that ended ran as its owner, with nothing left to revoke, and shows nothing for a run that never did", () => {
    const first = show(<RunAccess run={{ ...RUN, state: "done", as_owner_since: null }} owner />);
    expect(screen.getByTestId("run-as-owner")).toHaveTextContent("Đã chạy như octo");
    expect(screen.getByTestId("run-access-since")).toHaveTextContent("không ghi nhận");
    expect(screen.getByTestId("run-access-ends")).toHaveTextContent("đã kết thúc cùng run");
    expect(screen.queryByTestId("run-access-revoke")).toBeNull();
    first.view.unmount();

    show(
      <>
        <RunAccess run={{ ...RUN, as_owner: false, as_owner_since: null }} owner />
        <AsOwnerBadge run={{ ...RUN, as_owner: false }} />
      </>,
    );
    expect(screen.queryByTestId("run-access")).toBeNull();
    expect(screen.queryByTestId("run-as-owner")).toBeNull();
  });

  it("revokes after a confirmation with the CSRF header, then says the token ended", async () => {
    api.answer = (request) =>
      request.method === "PUT" ? Response.json({ ...RUN, as_owner: false, as_owner_since: null }) : Response.json({ error: "not_found", message: "-" }, { status: 404 });
    const { user } = show(<RunAccess run={RUN} owner />);
    await user.click(screen.getByTestId("run-access-revoke"));
    const dialog = screen.getByTestId("run-access-dialog");
    expect(dialog).toHaveTextContent("Dừng run #12 chạy như bạn?");
    expect(dialog).toHaveTextContent("evo-agents hub run access 12 --as-me");
    expect(api.seen.filter((request) => request.method === "PUT")).toHaveLength(0);
    await user.click(within(dialog).getByTestId("run-access-dialog-confirm"));
    await screen.findByText("Run #12 không còn chạy như bạn");
    const [put] = api.seen.filter((request) => request.method === "PUT");
    expect(put.url).toBe("http://hub.test/v1/projects/demo/runs/12/access");
    expect(put.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(JSON.parse(put.body)).toEqual({ as_me: false });
    await waitFor(() => expect(screen.queryByTestId("run-access-dialog")).toBeNull());
  });

  it("keeps the confirmation open with the reason when the hub refuses", async () => {
    api.answer = () => Response.json({ error: "forbidden", message: "only octo, who dispatched run 12, may change the access of it", request_id: "rid-3" }, { status: 403 });
    const { user } = show(<RunAccess run={RUN} owner />);
    await user.click(screen.getByTestId("run-access-revoke"));
    await user.click(screen.getByTestId("run-access-dialog-confirm"));
    const dialog = screen.getByTestId("run-access-dialog");
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("rid-3");
  });
});

describe("the Credentials card of a run that acts as its owner", () => {
  it("shows the run's own token of the hub as Hub account, with the variable it sets and never a value", async () => {
    api.leases = [
      { id: 3, name: "run-access:octo", provider: "run-access", kind: "hub", target: "EVO_HUB_TOKEN", worker: "laptop", issued_at: "2026-10-27T03:01:00Z", expires_at: null, revoked_at: null },
    ];
    show(<RunCredentials run={RUN} />);
    const [item] = await screen.findAllByTestId("run-lease-item");
    expect(within(item).getByTestId("run-lease-provider")).toHaveTextContent("Tài khoản hub");
    expect(within(item).getByTestId("run-lease-target")).toHaveTextContent(/^EVO_HUB_TOKEN$/);
    expect(item).toHaveTextContent("Biến");
    expect(within(item).queryByRole("link")).toBeNull();
  });
});
