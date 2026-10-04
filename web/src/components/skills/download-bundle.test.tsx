// @vitest-environment jsdom
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { DownloadBundle } from "./download-bundle";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function apiAnswering(answer: (request: Request) => Response) {
  const seen: string[] = [];
  const api = createApiClient({
    baseUrl: "http://web.test",
    fetch: async (request) => {
      seen.push(request.url);
      return answer(request);
    },
  });
  return { api: () => api, seen };
}

const TICKET = {
  scope: "project",
  project: "demo",
  name: "team-notes",
  version: 2,
  sha256: "a".repeat(64),
  size: 10,
  url: "https://bucket.example/blobs/sha256/aaaa?X-Amz-Signature=x",
  expires_at: "2026-10-04T00:05:00Z",
};

describe("DownloadBundle", () => {
  it("asks the API for the version's URL and sends the browser there, without fetching it", async () => {
    const { api, seen } = apiAnswering(() => json(TICKET));
    const navigate = vi.fn();
    renderVi(
      <DownloadBundle
        place={{ kind: "project", project: "demo" }}
        name="team-notes"
        version={2}
        blobStore="ok"
        api={api}
        navigate={navigate}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Tải bundle v2" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith(TICKET.url));
    expect(seen).toEqual(["http://web.test/v1/skills/projects/demo/team-notes/bundle?version=2"]);
    expect(screen.getByText("Trình duyệt đang tải team-notes-v2.tar.gz.")).toBeInTheDocument();
  });

  it("is off, and says why, when the hub has no blob store", () => {
    const { api, seen } = apiAnswering(() => json(TICKET));
    renderVi(
      <DownloadBundle place={{ kind: "global" }} name="house-style" version={1} blobStore="unconfigured" api={api} />,
    );
    const button = screen.getByRole("button", { name: "Tải bundle v1" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Hub chưa cấu hình kho blob");
    expect(seen).toEqual([]);
  });

  it.each([
    [403, "Bạn không có quyền tải bundle này. Nhờ quản trị viên dự án cấp grant."],
    [404, "Phiên bản này không còn trên hub. Tải lại trang để xem danh sách mới."],
    [503, "Kho blob của hub chưa sẵn sàng nên chưa tải được. Thử lại sau, hoặc báo quản trị viên hub."],
    [500, "Không tải được (mã 500, request rid-7)."],
  ])("says what a %i means and navigates nowhere", async (status, message) => {
    const { api } = apiAnswering(() => json({ error: "x", message: "from the API", request_id: "rid-7" }, status));
    const navigate = vi.fn();
    renderVi(
      <DownloadBundle
        place={{ kind: "global" }}
        name="house-style"
        version={1}
        blobStore="ok"
        compact
        api={api}
        navigate={navigate}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Tải bundle house-style phiên bản 1" }));
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
  });
});
