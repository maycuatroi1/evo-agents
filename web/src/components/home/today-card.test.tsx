// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { HubQueryState } from "@/components/states/query-view";
import { createApiClient } from "@/lib/api/client";
import type { Today } from "@/lib/notification-queries";
import { renderVi } from "@/test/render";

import en from "../../../messages/en.json";
import { TodayCard } from "./today-card";

const api = vi.hoisted(() => ({ seen: [] as { path: string; body: string }[], fail: false }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        if (path === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        api.seen.push({ path, body: await request.clone().text() });
        if (api.fail) return Response.json({ error: "server", message: "down" }, { status: 503 });
        return Response.json({ seen_at: "2026-10-25T03:00:00Z" });
      },
    }),
}));

/** An IntersectionObserver the test drives: `show` says the observed line came into view. */
class FakeObserver {
  static last: FakeObserver | null = null;
  constructor(private readonly callback: IntersectionObserverCallback) {
    FakeObserver.last = this;
  }
  observe = vi.fn();
  disconnect = vi.fn();
  show(visible = true) {
    this.callback([{ isIntersecting: visible } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
  }
}

const TODAY: Today = {
  summary: "1 question waits for your answer. Since you last looked: a release, 2 failed runs and $3.20 spent by your runs.",
  since: "2026-10-24T08:00:00Z",
  seen_at: "2026-10-24T08:00:00Z",
  seen_until: "2026-10-25T02:00:00Z",
  needs_you: [],
  needs_you_count: 1,
  items: [
    { kind: "release", title: "northwind released v1.4.0", body: "The daily cycle published it.", at: "2026-10-24T09:00:00Z", project: "northwind", link: "/p/northwind/releases" },
    { kind: "run_failed", title: "Run #41 failed", body: "verify failed", at: "2026-10-24T10:00:00Z", project: "northwind", link: "/p/northwind/runs/41" },
    { kind: "run_failed", title: "Run #42 lost", body: null, at: "2026-10-24T11:00:00Z", project: "lumen", link: "/p/lumen/runs/42" },
    { kind: "cost", title: "Your runs cost $3.20", body: "2 runs you dispatched ended in lumen.", at: "2026-10-25T02:00:00Z", project: "lumen", link: "/p/lumen/runs" },
  ],
  caught_up: true,
};

const success = (today: Today): HubQueryState<Today> => ({ status: "success", data: today });

function wrap(ui: ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

beforeEach(() => {
  api.seen = [];
  api.fail = false;
  FakeObserver.last = null;
  vi.stubGlobal("IntersectionObserver", FakeObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TodayCard", () => {
  it("says what waits and what changed in Vietnamese, lists each change as a link, and that this is everything", () => {
    renderVi(wrap(<TodayCard state={success(TODAY)} />));
    const card = screen.getByRole("region", { name: "Today" });
    expect(within(card).getByTestId("today-window")).toHaveTextContent("Từ lần bạn xem trước");
    expect(within(card).getByTestId("today-summary")).toHaveTextContent(
      "1 câu hỏi chờ bạn trả lời. Từ lần bạn xem trước: 1 bản phát hành, 2 run thất bại và chi phí run của bạn ở 1 dự án.",
    );
    const links = within(card).getAllByTestId("today-item-link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/p/northwind/releases", "/p/northwind/runs/41", "/p/lumen/runs/42", "/p/lumen/runs"]);
    expect(links[1]).toHaveAccessibleName("Run thất bại: Run #41 failed");
    expect(within(card).getByTestId("today-caught-up")).toHaveTextContent("Bạn đã xem hết.");
    expect(within(card).getByTestId("today-settings")).toHaveAttribute("href", "/settings/notifications");
  });

  it("shows the hub's own sentence in English, and says when more changed than it lists", () => {
    render(
      <NextIntlClientProvider locale="en" messages={en} timeZone="Asia/Ho_Chi_Minh">
        {wrap(<TodayCard state={success({ ...TODAY, seen_at: null, caught_up: false })} />)}
      </NextIntlClientProvider>,
    );
    expect(screen.getByTestId("today-summary")).toHaveTextContent(TODAY.summary);
    expect(screen.getByTestId("today-window")).toHaveTextContent("Last 24 hours");
    expect(screen.getByTestId("today-more")).toHaveTextContent("More changed than this. The rest shows next time.");
  });

  it("moves the seen mark to what it listed once its last line shows, once, and not while the page is hidden", async () => {
    const hidden = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    renderVi(wrap(<TodayCard state={success({ ...TODAY, caught_up: false })} />));
    const observer = FakeObserver.last!;
    expect(observer.observe).toHaveBeenCalledWith(screen.getByTestId("today-more"));
    act(() => observer.show());
    expect(api.seen).toEqual([]);

    hidden.mockReturnValue("visible");
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    await waitFor(() => expect(api.seen).toEqual([{ path: "/v1/me/today/seen", body: JSON.stringify({ seen_at: TODAY.seen_until }) }]));
    act(() => observer.show());
    act(() => observer.show());
    await Promise.resolve();
    expect(api.seen).toHaveLength(1);
  });

  it("does not move the mark before the end shows, and tries again after a failed write", async () => {
    api.fail = true;
    renderVi(wrap(<TodayCard state={success(TODAY)} />));
    const observer = FakeObserver.last!;
    act(() => observer.show(false));
    expect(api.seen).toEqual([]);
    act(() => observer.show());
    await waitFor(() => expect(api.seen).toHaveLength(1));
    api.fail = false;
    await waitFor(() => {
      act(() => observer.show());
      expect(api.seen).toHaveLength(2);
    });
  });

  it("keeps a failed read inside its card, with Try again", async () => {
    const retry = vi.fn();
    renderVi(wrap(<TodayCard state={{ status: "error", error: { status: 503, code: "server", message: "down", requestId: null }, retry }} />));
    const error = screen.getByTestId("today-error");
    expect(error).toHaveTextContent("Không đọc được Today từ hub.");
    await userEvent.setup().click(within(error).getByRole("button", { name: "Thử lại" }));
    expect(retry).toHaveBeenCalled();
    expect(api.seen).toEqual([]);
  });
});
