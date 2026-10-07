// @vitest-environment jsdom
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { Route } from "next";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { notify, notifyFailure, TOAST_MS } from "./toast";

vi.mock("next/navigation", () => ({ usePathname: () => "/p/demo/runs" }));

afterEach(() => {
  vi.useRealTimers();
});

describe("toasts", () => {
  it("says a success in the past tense with a link back to what changed, and closes after 5 seconds", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    renderVi(<p>page</p>);
    act(() => {
      notify({
        tone: "success",
        text: "Đã dispatch run #13",
        description: "Run chờ trong queue tới khi có worker nhận.",
        link: { label: "Mở run", href: "/p/demo/runs/13" as Route },
      });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0); // Sonner adds a toast on the next tick
    });
    const toast = screen.getByTestId("toast");
    expect(toast).toHaveAttribute("data-tone", "success");
    expect(toast).not.toHaveAttribute("role", "alert");
    expect(within(toast).getByTestId("toast-title")).toHaveTextContent("Đã dispatch run #13");
    expect(within(toast).getByRole("link", { name: "Mở run" })).toHaveAttribute("href", "/p/demo/runs/13");
    // Sonner's list is a polite live region, named in the visitor's language.
    expect(toast.closest("section")).toHaveAttribute("aria-live", "polite");
    expect(toast.closest("section")?.getAttribute("aria-label")).toMatch(/^Thông báo/);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TOAST_MS + 1_000);
    });
    expect(screen.queryByTestId("toast")).toBeNull();
  });

  it("keeps a failure as an alert with what to do and the request id until it is dismissed", async () => {
    renderVi(<p>page</p>);
    act(() => {
      notifyFailure(
        "Không chạy lại được #4",
        { text: "Plan đã đổi từ khi run này chạy.", detail: "Hub báo: plan changed", requestId: "rid-7", status: 409 },
        { label: "Mở plan", href: "/p/demo/plans/rollout" as Route },
      );
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveAttribute("data-tone", "error");
    expect(alert).toHaveTextContent("Không chạy lại được #4");
    expect(alert).toHaveTextContent("Plan đã đổi từ khi run này chạy.");
    expect(alert).toHaveTextContent("Hub báo: plan changed");
    expect(within(alert).getByTestId("toast-request-id")).toHaveTextContent("Mã request: rid-7");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole("button", { name: "Đóng thông báo" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });

  it("leaves the link out while its page is the one shown", async () => {
    renderVi(<p>page</p>);
    act(() => {
      notify({ tone: "success", text: "Đã đánh dấu thông báo là đã đọc", link: { label: "Mở run", href: "/p/demo/runs?state=active" as Route } });
    });
    expect(within(await screen.findByTestId("toast")).queryByRole("link")).toBeNull();
  });
});
