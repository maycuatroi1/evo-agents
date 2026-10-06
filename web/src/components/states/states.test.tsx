// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { ApiErrorState, LoadingState, NoResults } from "./states";

const info = (status: number, requestId: string | null = "rid-42") => ({
  status,
  code: status === 403 ? "forbidden" : status === 404 ? "not_found" : "internal_error",
  message: "from the API",
  requestId,
});

describe("ApiErrorState", () => {
  it("says the visitor has no access on a 403", () => {
    renderVi(<ApiErrorState error={info(403)} />);
    expect(screen.getByRole("heading", { name: "Bạn không có quyền xem trang này" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Về danh sách dự án" })).toHaveAttribute("href", "/");
  });

  it("says not found on a 404", () => {
    renderVi(<ApiErrorState error={info(404)} />);
    expect(screen.getByRole("heading", { name: "Không tìm thấy trang" })).toBeInTheDocument();
  });

  it("shows the request id and a retry on a 5xx", async () => {
    const retry = vi.fn();
    renderVi(<ApiErrorState error={info(500)} onRetry={retry} />);
    expect(screen.getByRole("heading", { name: "Hub gặp lỗi khi xử lý yêu cầu" })).toBeInTheDocument();
    expect(screen.getByText("rid-42")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Thử lại" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("names a lost connection when nothing answered", () => {
    renderVi(<ApiErrorState error={{ status: 0, code: "network", message: "fetch failed", requestId: null }} />);
    expect(screen.getByRole("heading", { name: "Không kết nối được tới API của hub" })).toBeInTheDocument();
    expect(screen.queryByText("Mã request:")).not.toBeInTheDocument();
  });
});

describe("LoadingState", () => {
  it("announces loading once and hides the skeleton from screen readers", () => {
    renderVi(<LoadingState />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent("Đang tải…");
    // The skeleton fades in only after 300 ms, so a quick answer never flashes it.
    expect(status.querySelector("[aria-hidden=true]")).toHaveClass("animate-appear-late");
  });
});

describe("NoResults", () => {
  it("names the filters in use and clears them", async () => {
    const onClear = vi.fn();
    renderVi(
      <NoResults
        title="Không có run nào khớp"
        filters={[
          { label: "Trạng thái", value: "Hoàn tất" },
          { label: "Tìm", value: "credentials" },
        ]}
        onClear={onClear}
      />,
    );
    const state = screen.getByTestId("state-empty");
    expect(screen.getByRole("heading", { name: "Không có run nào khớp" })).toBeInTheDocument();
    expect(state).toHaveTextContent("Bộ lọc đang dùng");
    const used = screen.getAllByRole("listitem").map((item) => item.textContent);
    expect(used).toEqual(["Trạng thái:Hoàn tất", "Tìm:credentials"]);
    await userEvent.click(screen.getByRole("button", { name: "Xoá bộ lọc" }));
    expect(onClear).toHaveBeenCalledOnce();
  });
});
