// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WriteFailure } from "@/components/feedback/notice";
import { createApiClient } from "@/lib/api/client";
import { chatKeys } from "@/lib/chat-queries";
import type { Chat } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { ChatLabelControl } from "./chat-label-control";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { url: string; method: string; body: string }[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        api.seen.push({ url: request.url, method: request.method, body: await request.clone().text() });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

const INTERNAL = { level: "internal", location: "any", integrity: "U", projects: ["northwind"] };

const CHAT: Chat = {
  id: 7,
  title: "Northwind bridge",
  created_by: "octo",
  created_at: "2026-10-27T08:00:00Z",
  people: [{ login: "octo", added_by: "octo", added_at: "2026-10-27T08:00:00Z", avatar_url: null }],
  agents: [{ handle: "reviewer", project: "northwind", name: "Reviewer", color: "#2563eb", icon_url: null, is_default: false, deleted: false, added_by: "octo", added_at: "2026-10-27T08:00:00Z" }],
  label: [INTERNAL],
  label_level: null,
  label_levels: ["public", "internal"],
  label_admin: true,
  restricted: false,
  last_seq: 3,
  last_read_seq: 3,
  kind: "group",
  image_url: null,
};

function setup(chat: Chat = CHAT) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  const problems: (WriteFailure | null)[] = [];
  renderVi(
    <QueryClientProvider client={client}>
      <ChatLabelControl chat={chat} onProblem={(problem) => problems.push(problem)} />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), client, problems };
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("the level of a chat's label in its head", () => {
  it("is drawn for an admin of every project of a group chat's agents only", () => {
    const { unmount } = renderVi(
      <QueryClientProvider client={new QueryClient()}>
        <ChatLabelControl chat={{ ...CHAT, label_admin: false }} onProblem={vi.fn()} />
        <ChatLabelControl chat={{ ...CHAT, label_levels: [] }} onProblem={vi.fn()} />
        <ChatLabelControl chat={{ ...CHAT, kind: "direct" }} onProblem={vi.fn()} />
      </QueryClientProvider>,
    );
    expect(screen.queryByTestId("chat-label-open")).toBeNull();
    unmount();
  });

  it("offers the projects' default and the levels the hub allows, and sets one", async () => {
    const lowered = { ...CHAT, label: [{ ...INTERNAL, level: "public" }], label_level: "public" };
    api.answer = () => Response.json(lowered);
    const { user, client } = setup();
    await user.click(screen.getByRole("button", { name: "Mức của chat: mặc định" }));
    const menu = await screen.findByRole("menu");
    const items = within(menu).getAllByRole("menuitemradio");
    expect(items.map((item) => item.textContent)).toEqual(["Mặc định của các project", "public", "internal"]);
    expect(items[0]).toHaveAttribute("aria-checked", "true");
    await user.click(within(menu).getByRole("menuitemradio", { name: "public" }));
    await waitFor(() => expect(client.getQueryData(chatKeys.one(7))).toEqual(lowered));
    expect(api.seen).toEqual([{ url: "http://hub.test/v1/chats/7/label", method: "PUT", body: JSON.stringify({ level: "public" }) }]);
    expect(await screen.findByText("Đã đặt mức của chat là public")).toBeInTheDocument();
  });

  it("takes the level off with the projects' default, and says the hub's refusal", async () => {
    api.answer = () => Response.json({ error: "conflict", message: "lumen-reader would no longer clear the label" }, { status: 409 });
    const { user, problems } = setup({ ...CHAT, label_level: "public" });
    await user.click(screen.getByRole("button", { name: "Mức của chat: public" }));
    await user.click(within(await screen.findByRole("menu")).getByRole("menuitemradio", { name: "Mặc định của các project" }));
    await waitFor(() => expect(problems.at(-1)).toBeTruthy());
    expect(api.seen.map((seen) => [seen.method, seen.url])).toEqual([["DELETE", "http://hub.test/v1/chats/7/label"]]);
    expect(problems.at(-1)?.detail).toContain("lumen-reader would no longer clear the label");
  });
});
