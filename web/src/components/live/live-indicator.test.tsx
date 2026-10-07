// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useHubQuery } from "@/components/states/query-view";
import { ApiError } from "@/lib/api/errors";

import messages from "../../../messages/vi.json";

import { LiveProvider, useLiveSignal } from "./live-context";
import { LiveIndicator } from "./live-indicator";
import { INITIAL_LIVE, type LiveSignal, OFFLINE_AFTER_MS, querySignal, stepLive } from "./live-model";

const START = Date.parse("2026-10-07T03:00:00Z");

function stream(overrides: Partial<LiveSignal> = {}): LiveSignal {
  return {
    transport: "stream",
    updatedAt: Date.now(),
    failing: false,
    degraded: false,
    offline: false,
    paused: false,
    retryAt: null,
    pollMs: null,
    ...overrides,
  };
}

function Source({ signal, resume, retry }: { signal: LiveSignal | null; resume?: () => void; retry?: () => void }) {
  useLiveSignal(signal, { resume, retry });
  return null;
}

function Shell({ client, children }: { client: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <LiveProvider>
        {children}
        <LiveIndicator />
      </LiveProvider>
    </QueryClientProvider>
  );
}

/** Vietnamese, as `renderVi`, kept around the tree through `rerender`. */
function renderVi(ui: ReactElement) {
  return render(ui, {
    wrapper: ({ children }) => (
      <NextIntlClientProvider locale="vi" messages={messages} timeZone="Asia/Ho_Chi_Minh">
        {children}
      </NextIntlClientProvider>
    ),
  });
}

const indicator = () => screen.getByTestId("live-indicator");
const status = () => screen.getByRole("status");
const detail = () => screen.getByTestId("live-detail");

/** Lets the shared clock tick `ms` forward, a second at a time, as the browser would. */
async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(START);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("stepLive", () => {
  it("goes Reconnecting on a failure, Offline after OFFLINE_AFTER_MS of failures, and stays there until data arrives", () => {
    const failing = stream({ failing: true });
    let machine = stepLive(INITIAL_LIVE, stream(), START);
    expect(machine).toBe(INITIAL_LIVE); // nothing changed: the same object, so the indicator does not render again
    machine = stepLive(machine, failing, START + 1_000);
    expect(machine).toEqual({ state: "reconnecting", failingSince: START + 1_000 });
    machine = stepLive(machine, failing, START + 1_000 + OFFLINE_AFTER_MS - 1);
    expect(machine.state).toBe("reconnecting");
    machine = stepLive(machine, failing, START + 1_000 + OFFLINE_AFTER_MS);
    expect(machine.state).toBe("offline");
    machine = stepLive(machine, failing, START + 60_000);
    expect(machine.state).toBe("offline");
    expect(stepLive(machine, stream(), START + 61_000)).toEqual({ state: "live", failingSince: null });
  });

  it("says Offline at once when the browser has no network, Paused whatever else holds, Reconnecting while degraded", () => {
    expect(stepLive(INITIAL_LIVE, stream({ offline: true }), START).state).toBe("offline");
    expect(stepLive(INITIAL_LIVE, stream({ paused: true, failing: true }), START).state).toBe("paused");
    const degraded = stepLive(INITIAL_LIVE, stream({ degraded: true }), START);
    expect(degraded).toEqual({ state: "reconnecting", failingSince: null });
    // Updates still arrive by the fallback: it never turns Offline on its own.
    expect(stepLive(degraded, stream({ degraded: true }), START + 10 * OFFLINE_AFTER_MS).state).toBe("reconnecting");
  });

  it("reads a polling query's failures from its state in the cache", () => {
    const base = {
      data: 1,
      dataUpdateCount: 1,
      dataUpdatedAt: START,
      error: null,
      errorUpdateCount: 0,
      errorUpdatedAt: 0,
      fetchFailureCount: 0,
      fetchFailureReason: null,
      fetchMeta: null,
      isInvalidated: false,
      status: "success" as const,
      fetchStatus: "idle" as const,
    };
    expect(querySignal(base, 5_000)).toMatchObject({ failing: false, updatedAt: START, pollMs: 5_000, retryAt: null });
    // A retry under way: failing, and the next try is now.
    expect(querySignal({ ...base, fetchFailureCount: 1, fetchStatus: "fetching" }, 5_000)).toMatchObject({ failing: true, retryAt: null });
    // The retries gave up: the next try is one interval after the failure.
    const gaveUp = { ...base, errorUpdatedAt: START + 3_000, errorUpdateCount: 1, fetchFailureCount: 3 };
    expect(querySignal(gaveUp, 5_000)).toMatchObject({ failing: true, retryAt: START + 8_000 });
    // The next interval's fetch starts afresh (no failure counted yet), but the last answer is still an error.
    expect(querySignal({ ...gaveUp, fetchFailureCount: 0, fetchStatus: "fetching" }, 5_000).failing).toBe(true);
    expect(querySignal({ ...base, fetchStatus: "paused" }, 5_000).offline).toBe(true);
  });
});

describe("LiveIndicator", () => {
  it("shows nothing while the page registered nothing", () => {
    renderVi(<Shell client={new QueryClient()}>{null}</Shell>);
    expect(screen.queryByTestId("live-indicator")).toBeNull();
  });

  it("ticks 'updated N ago' on the shared clock without announcing it, and announces each change of state", async () => {
    const client = new QueryClient();
    const retry = vi.fn();
    const signal = stream();
    const { rerender } = renderVi(
      <Shell client={client}>
        <Source signal={signal} retry={retry} />
      </Shell>,
    );
    await wait(0);
    expect(indicator()).toHaveAttribute("data-state", "live");
    expect(status()).toHaveTextContent("Cập nhật: Trực tiếp");
    expect(detail()).toHaveTextContent("cập nhật vừa xong");
    const region = status();

    await wait(3_000);
    expect(detail()).toHaveTextContent("cập nhật 3 giây trước");
    // The clock is outside the status region: the region and its words are the same, so nothing is read out.
    expect(status()).toBe(region);
    expect(region).toHaveTextContent("Cập nhật: Trực tiếp");
    expect(region).not.toHaveTextContent("giây");

    // The stream breaks: Reconnecting, said once.
    rerender(
      <Shell client={client}>
        <Source signal={{ ...signal, failing: true }} retry={retry} />
      </Shell>,
    );
    await wait(0);
    expect(indicator()).toHaveAttribute("data-state", "reconnecting");
    expect(status()).toHaveTextContent("Cập nhật: Đang kết nối lại");
    expect(detail()).toHaveTextContent("đang nối lại luồng");

    // Still failing after OFFLINE_AFTER_MS: Offline, with the time of the last update and Retry now.
    await wait(OFFLINE_AFTER_MS - 1_000);
    expect(indicator()).toHaveAttribute("data-state", "reconnecting");
    await wait(1_000);
    expect(indicator()).toHaveAttribute("data-state", "offline");
    expect(status()).toHaveTextContent("Cập nhật: Mất kết nối");
    expect(detail()).toHaveTextContent("lần cập nhật cuối 18 giây trước");
    fireEvent.click(screen.getByRole("button", { name: "Thử lại ngay" }));
    expect(retry).toHaveBeenCalledOnce();

    // An update arrives again: Live.
    rerender(
      <Shell client={client}>
        <Source signal={stream()} retry={retry} />
      </Shell>,
    );
    await wait(0);
    expect(indicator()).toHaveAttribute("data-state", "live");
    expect(screen.queryByRole("button", { name: "Thử lại ngay" })).toBeNull();
  });

  it("says when the stream is tried again and how the page reads meanwhile", async () => {
    renderVi(
      <Shell client={new QueryClient()}>
        <Source signal={stream({ degraded: true, retryAt: START + 25_000, pollMs: 1_500 })} />
      </Shell>,
    );
    await wait(0);
    expect(indicator()).toHaveAttribute("data-state", "reconnecting");
    expect(detail()).toHaveTextContent("thử lại luồng sau 25 giây, trong lúc chờ đọc lại mỗi 1,5 giây");
    await wait(5_000);
    expect(detail()).toHaveTextContent("thử lại luồng sau 20 giây");
  });

  it("says Paused while the person holds the log, and Resume gives it back", async () => {
    const resume = vi.fn();
    renderVi(
      <Shell client={new QueryClient()}>
        <Source signal={stream({ paused: true })} resume={resume} />
      </Shell>,
    );
    await wait(0);
    expect(indicator()).toHaveAttribute("data-state", "paused");
    expect(status()).toHaveTextContent("Cập nhật: Tạm dừng");
    expect(detail()).toHaveTextContent("do bạn dừng");
    fireEvent.click(screen.getByRole("button", { name: "Tiếp tục" }));
    expect(resume).toHaveBeenCalledOnce();
  });

  it("follows the page's main query: Live, Reconnecting while it fails, Offline, then Live again on Retry now", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let up = true;
    const fetches = vi.fn(async () => {
      if (!up) throw new ApiError({ status: 0, code: "network", message: "no answer", requestId: null });
      return { ok: true };
    });
    function Page() {
      const state = useHubQuery({ queryKey: ["live-test"], queryFn: fetches, refetchInterval: 5_000 }, null, { live: true });
      return <p data-testid="page">{state.status}</p>;
    }
    renderVi(
      <Shell client={client}>
        <Page />
      </Shell>,
    );
    await wait(0);
    expect(indicator()).toHaveAttribute("data-state", "live");
    expect(indicator()).toHaveAttribute("data-transport", "poll");

    up = false;
    await wait(5_000); // the next poll fails
    expect(indicator()).toHaveAttribute("data-state", "reconnecting");
    expect(detail()).toHaveTextContent("thử lại sau 5 giây, đọc lại mỗi 5 giây");

    await wait(OFFLINE_AFTER_MS);
    expect(indicator()).toHaveAttribute("data-state", "offline");
    expect(detail()).toHaveTextContent("lần cập nhật cuối 20 giây trước");
    // The page keeps what it last read; the top bar is what says it is not current.
    expect(screen.getByTestId("page")).toHaveTextContent("success");

    up = true;
    const before = fetches.mock.calls.length;
    fireEvent.click(screen.getByRole("button", { name: "Thử lại ngay" }));
    await wait(0);
    expect(fetches.mock.calls.length).toBeGreaterThan(before);
    expect(indicator()).toHaveAttribute("data-state", "live");
    expect(detail()).toHaveTextContent("cập nhật vừa xong");
  });

  it("leaves the top bar when the page that registered goes away", async () => {
    const client = new QueryClient();
    const { rerender } = renderVi(
      <Shell client={client}>
        <Source signal={stream()} />
      </Shell>,
    );
    await wait(0);
    expect(screen.getByTestId("live-indicator")).toBeInTheDocument();
    rerender(<Shell client={client}>{null}</Shell>);
    await wait(0);
    expect(screen.queryByTestId("live-indicator")).toBeNull();
  });
});
