// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { CuratorBuildDialog } from "./curator-build-dialog";
import type { Run } from "./queries";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as Request[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

const PLAN = "curator-45-learning-children-off-hub";
const BUILD = `/v1/projects/demo/curator/plans/${PLAN}/build`;
const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-9" } });

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

function Harness({ onDispatched }: { onDispatched: (run: Run) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <CuratorBuildDialog project="demo" planId={PLAN} open={open} onOpenChange={setOpen} onDispatched={onDispatched} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

describe("CuratorBuildDialog", () => {
  it("says the Builder runs by the Curator's rules and queues it with the CSRF header", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    const queued = { id: 721, kind: "plan", project: "demo", plan_id: PLAN, state: "queued" } as Run;
    api.answer = (request) => (new URL(request.url).pathname === BUILD && request.method === "POST" ? json(queued, 201) : json({}, 404));
    renderVi(<Harness onDispatched={dispatched} />);

    const dialog = screen.getByRole("alertdialog", { name: "Chạy Builder của Curator ngay?" });
    expect(dialog).toHaveTextContent(PLAN);
    expect(dialog).toHaveTextContent("worker đang trực");
    await user.click(within(dialog).getByTestId("curator-build-confirm"));

    await vi.waitFor(() => expect(dispatched).toHaveBeenCalledWith(queued));
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
  });

  it("keeps the dialog open with the hub's reason when the hub refuses", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    const why = "the night shift is paused: resume it first; nothing was queued";
    api.answer = () => json({ error: "conflict", message: why }, 409);
    renderVi(<Harness onDispatched={dispatched} />);

    const dialog = screen.getByRole("alertdialog");
    await user.click(within(dialog).getByTestId("curator-build-confirm"));
    const alert = await within(dialog).findByTestId("admin-dialog-error");
    expect(alert).toHaveTextContent("Hub không xếp Builder.");
    expect(alert).toHaveTextContent(`Hub báo: ${why}`);
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
    expect(dispatched).not.toHaveBeenCalled();
  });
});
