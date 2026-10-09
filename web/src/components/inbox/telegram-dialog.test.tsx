// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { TelegramButton } from "./telegram-dialog";
import type { TelegramStatus } from "./telegram";

const api = vi.hoisted(() => ({
  status: null as null | Record<string, unknown>,
  linkAnswer: null as null | (() => Response),
  seen: [] as string[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        api.seen.push(`${request.method} ${path}`);
        if (path === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (path === "/v1/me/telegram" && request.method === "GET") return Response.json(api.status);
        if (path === "/v1/me/telegram" && request.method === "DELETE") {
          api.status = { ...api.status, linked: false, enabled: false, username: null, linked_at: null };
          return Response.json(api.status);
        }
        if (path === "/v1/me/telegram/link") return api.linkAnswer ? api.linkAnswer() : Response.json({}, { status: 500 });
        return Response.json({ error: "not_found", message: "no route" }, { status: 404 });
      },
    }),
}));

const NOT_LINKED: TelegramStatus = {
  configured: true,
  linked: false,
  enabled: false,
  username: null,
  linked_at: null,
  disabled_reason: null,
  bot: null,
};
const LINKED: TelegramStatus = { ...NOT_LINKED, linked: true, enabled: true, username: "octo_tg", linked_at: "2026-10-09T06:30:00Z", bot: "evo_hub_bot" };

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <TelegramButton />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByTestId("inbox-telegram"));
  return screen.findByRole("dialog", { name: "Telegram" });
}

beforeEach(() => {
  api.status = { ...NOT_LINKED };
  api.linkAnswer = null;
  api.seen = [];
});

describe("TelegramDialog", () => {
  it("makes a one-time link, shows it with its expiry, and is linked once the hub says so", async () => {
    const expires = new Date(Date.now() + 10 * 60_000).toISOString();
    api.linkAnswer = () => Response.json({ url: "https://t.me/evo_hub_bot?start=" + "c".repeat(32), bot: "evo_hub_bot", expires_at: expires }, { status: 201 });
    const user = setup();
    const dialog = await openDialog(user);
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "unlinked"));
    expect(within(dialog).getByTestId("telegram-unlinked")).toHaveTextContent("Chưa liên kết cuộc trò chuyện nào");

    await user.click(within(dialog).getByTestId("telegram-make-link"));
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "pending"));
    expect(within(dialog).getByTestId("telegram-link")).toHaveTextContent("https://t.me/evo_hub_bot?start=");
    const open = within(dialog).getByTestId("telegram-open");
    expect(open).toHaveAttribute("href", "https://t.me/evo_hub_bot?start=" + "c".repeat(32));
    expect(open).toHaveAttribute("target", "_blank");
    expect(open).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(dialog).getByTestId("telegram-waiting")).toHaveTextContent("Đang chờ Telegram");
    expect(within(dialog).getByText(/@evo_hub_bot/)).toBeInTheDocument();
    expect(api.seen).toContain("POST /v1/me/telegram/link");

    api.status = { ...LINKED };
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "linked"), { timeout: 5_000 });
    expect(within(dialog).getByTestId("telegram-linked")).toHaveTextContent("Đã liên kết với @octo_tg");
    expect(await screen.findByText("Đã liên kết Telegram")).toBeInTheDocument();
  });

  it("unlinks after a confirm step in place", async () => {
    api.status = { ...LINKED };
    const user = setup();
    const dialog = await openDialog(user);
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "linked"));
    await user.click(within(dialog).getByTestId("telegram-unlink"));
    expect(within(dialog).getByTestId("telegram-confirm-unlink")).toHaveTextContent("Huỷ liên kết cuộc trò chuyện này?");
    expect(api.seen).not.toContain("DELETE /v1/me/telegram");
    await user.click(within(dialog).getByTestId("telegram-unlink-confirm"));
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "unlinked"));
    expect(api.seen).toContain("DELETE /v1/me/telegram");
    expect(await screen.findByText("Đã huỷ liên kết Telegram")).toBeInTheDocument();
  });

  it("says when the hub has no bot, and when Telegram refused the chat", async () => {
    api.status = { ...NOT_LINKED, configured: false };
    const user = setup();
    let dialog = await openDialog(user);
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "unconfigured"));
    expect(within(dialog).getByTestId("telegram-unconfigured")).toHaveTextContent("Hub này chưa bật Telegram");
    expect(within(dialog).queryByTestId("telegram-make-link")).toBeNull();
    await user.click(within(dialog).getByTestId("telegram-close"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    api.status = { ...LINKED, enabled: false, disabled_reason: "Telegram refused the chat (Forbidden: bot was blocked by the user)" };
    dialog = await openDialog(user);
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "off"));
    expect(within(dialog).getByTestId("telegram-off")).toHaveTextContent("bot was blocked by the user");
    expect(within(dialog).getByTestId("telegram-make-link")).toHaveTextContent("Tạo liên kết mới");
  });

  it("keeps a refusal of the hub in the dialog", async () => {
    api.linkAnswer = () => Response.json({ error: "bad_gateway", message: "Telegram did not say who the hub's bot is" }, { status: 502 });
    const user = setup();
    const dialog = await openDialog(user);
    await waitFor(() => expect(dialog).toHaveAttribute("data-view", "unlinked"));
    await user.click(within(dialog).getByTestId("telegram-make-link"));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Telegram did not say who the hub's bot is");
    expect(dialog).toHaveAttribute("data-view", "unlinked");
  });
});
