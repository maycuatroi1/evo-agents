// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { MAX_MESSAGE_BYTES } from "./queries";
import { RunComposer } from "./run-composer";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { url: string; method: string; headers: Headers; body: string }[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push({ url: request.url, method: request.method, headers: request.headers, body: await request.clone().text() });
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <RunComposer run={{ project: "demo", id: 12 }} />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), box: screen.getByTestId("run-composer-text"), send: screen.getByTestId("run-composer-send") };
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("RunComposer", () => {
  it("refuses a blank message and one over 8 KiB of UTF-8 without asking the hub", async () => {
    const { user, box, send } = setup();
    await user.click(send);
    expect(screen.getByTestId("run-composer-problem")).toHaveTextContent("Hãy gõ tin nhắn trước.");
    expect(box).toHaveAttribute("aria-invalid", "true");
    await user.click(box);
    await user.paste("ệ".repeat(MAX_MESSAGE_BYTES / 3 + 1)); // 3 bytes each: just over the limit
    expect(screen.getByText(/trên 8\.192 byte/)).toBeInTheDocument();
    await user.click(send);
    expect(screen.getByTestId("run-composer-problem")).toHaveTextContent("tối đa 8.192 byte");
    expect(api.seen).toHaveLength(0);
  });

  it("sends the message with the session's CSRF header, then empties the box and says so", async () => {
    api.answer = () =>
      Response.json(
        { id: 3, run_id: 12, seq: 9, text: "x", sent_by: "octo", created_at: "2026-10-05T07:00:00Z", delivered_at: null },
        { status: 201 },
      );
    const { user, box, send } = setup();
    await user.type(box, "Phủ thêm token đã thu hồi");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(screen.getByTestId("run-composer-sent")).toHaveTextContent("Đã gửi."));
    expect(box).toHaveValue("");
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.url).toBe("http://hub.test/v1/projects/demo/runs/12/messages");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(JSON.parse(post?.body ?? "{}")).toEqual({ text: "Phủ thêm token đã thu hồi" });
    expect(send).not.toHaveAttribute("aria-disabled");
  });

  it("shows the hub's refusal next to the box, and keeps what was typed", async () => {
    api.answer = () =>
      Response.json({ error: "conflict", message: "run 12 is review: a message goes to a run that is queued or held", request_id: "rid-9" }, { status: 409 });
    const { user, box, send } = setup();
    await user.type(box, "còn đó không?");
    await user.click(send);
    const alert = await screen.findByTestId("admin-dialog-error");
    expect(alert).toHaveTextContent("Run không nhận tin nhắn nữa");
    expect(alert).toHaveTextContent("Hub báo: run 12 is review");
    expect(alert).toHaveTextContent("rid-9");
    expect(box).toHaveValue("còn đó không?");
  });
});
