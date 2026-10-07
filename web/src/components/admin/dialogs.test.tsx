// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Notice } from "@/components/feedback/toast";
import { createApiClient } from "@/lib/api/client";
import type { Project } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { ConfirmAction } from "./confirm-action";
import type { AdminUser } from "./data";
import { GrantDialog, type GrantPreset } from "./grant-dialog";

const api = vi.hoisted(() => ({ answer: null as null | ((request: Request) => Response | Promise<Response>), seen: [] as Request[] }));

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

const project = (name: string, levels: string[], level: string): Project => ({
  name,
  harness: null,
  levels,
  locations: ["any"],
  default_label: { level },
  sinks: [],
  repos: [],
  role: null,
  max_level: null,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
});
const PROJECTS = [project("demo", ["public", "internal", "secret"], "internal"), project("other", ["open", "closed"], "open")];
const USERS: AdminUser[] = [
  {
    login: "octo",
    admin: false,
    signed_in: true,
    created_at: "2026-10-01T00:00:00Z",
    last_seen_at: null,
    active_tokens: 1,
    grants: [{ project: "demo", role: "writer", max_level: "secret", granted_by: "admin", granted_at: "2026-10-01T00:00:00Z" }],
  },
];

function Harness({ preset = {}, onDone }: { preset?: GrantPreset; onDone: (notice: Notice) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <GrantDialog open={open} onOpenChange={setOpen} users={USERS} projects={PROJECTS} preset={preset} onDone={onDone} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-7" } });

/** jsdom has no ResizeObserver; Radix's radio group measures its items with one. */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  api.answer = null;
  api.seen = [];
});
afterEach(() => vi.unstubAllGlobals());

describe("GrantDialog", () => {
  it("checks the form, confirms, then grants with the CSRF header", async () => {
    const user = userEvent.setup();
    const done = vi.fn();
    api.answer = () => json({ project: "other", login: "newbie", role: "reader", max_level: "closed", created: true });
    renderVi(<Harness onDone={done} />);

    const dialog = screen.getByRole("dialog", { name: "Cấp quyền theo dự án" });
    await user.click(within(dialog).getByRole("button", { name: "Tiếp tục" }));
    expect(screen.getByText("Nhập tên đăng nhập GitHub.")).toBeInTheDocument();
    expect(screen.getByText("Chọn một dự án.")).toBeInTheDocument();
    const login = screen.getByLabelText("Tên đăng nhập GitHub");
    expect(login).toHaveFocus();
    expect(login).toHaveAttribute("aria-invalid", "true");

    await user.type(login, "newbie");
    await user.selectOptions(screen.getByLabelText("Dự án"), "other");
    expect((screen.getByLabelText("Mức hiển thị") as HTMLSelectElement).value).toBe("open"); // the project's default label
    await user.selectOptions(screen.getByLabelText("Mức hiển thị"), "closed");
    await user.click(screen.getByRole("radio", { name: "Đọc" }));
    await user.click(screen.getByRole("button", { name: "Tiếp tục" }));

    const summary = screen.getByTestId("grant-summary");
    expect(screen.getByRole("heading", { name: "Xác nhận cấp quyền" })).toHaveFocus();
    expect(summary).toHaveTextContent("newbie");
    expect(summary).toHaveTextContent("other");
    expect(summary).toHaveTextContent("closed"); // a level of the project's own ladder shows as written
    expect(api.seen).toHaveLength(0); // nothing written before the confirmation

    await user.click(screen.getByRole("button", { name: "Xác nhận cấp quyền" }));
    await waitFor(() => expect(done).toHaveBeenCalledOnce());
    expect(done.mock.calls[0][0]).toEqual({
      tone: "success",
      text: "Đã cấp quyền cho newbie",
      description: "Vai trò Đọc, mức hiển thị closed, cho newbie trong dự án other.",
      link: { label: "Mở thành viên", href: "/admin/members/newbie" },
    });
    const put = api.seen.find((request) => request.method === "PUT");
    expect(put?.url).toBe("http://hub.test/v1/admin/projects/other/grants/newbie");
    expect(put?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
  });

  it("says what the member has now when changing a grant, and refuses a change that changes nothing", async () => {
    const user = userEvent.setup();
    renderVi(<Harness onDone={vi.fn()} preset={{ login: "octo", lockLogin: true, project: "demo", role: "writer", maxLevel: "secret" }} />);
    expect(screen.queryByLabelText("Tên đăng nhập GitHub")).not.toBeInTheDocument(); // the login is fixed
    expect(screen.getByTestId("grant-existing")).toHaveTextContent("octo đã có đúng vai trò và mức hiển thị này");
    expect(screen.getByRole("button", { name: "Tiếp tục" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Mức hiển thị"), "internal");
    expect(screen.getByTestId("grant-existing")).toHaveTextContent("octo đang có vai trò Ghi, mức hiển thị Secret trong dự án này");
    await user.click(screen.getByRole("button", { name: "Tiếp tục" }));
    expect(screen.getByRole("heading", { name: "Xác nhận đổi quyền" })).toBeInTheDocument();
    expect(screen.getByTestId("grant-summary")).toHaveTextContent("Ghi, mức hiển thị Secret");
  });

  it.each([
    [422, { error: "invalid_request", message: "max-level must be a level of project demo" }, "Hub không chấp nhận dữ liệu này.", "Thông báo từ API: max-level must be a level of project demo"],
    [404, { error: "not_found", message: "project demo is not registered" }, "Dự án demo không còn trên hub.", null],
    [403, { error: "forbidden", message: "this needs a hub admin" }, "Hub từ chối thao tác", null],
    [500, { error: "internal_error", message: "boom" }, "Hub gặp lỗi khi lưu thay đổi.", null],
  ])("keeps the dialog open and explains a %i", async (status, body, text, detail) => {
    const user = userEvent.setup();
    const done = vi.fn();
    api.answer = () => json(body, status);
    renderVi(<Harness onDone={done} preset={{ login: "octo", lockLogin: true, project: "demo", role: "reader", maxLevel: "public" }} />);
    await user.click(screen.getByRole("button", { name: "Tiếp tục" }));
    await user.click(screen.getByRole("button", { name: "Xác nhận cấp quyền" }));
    const alert = await screen.findByTestId("admin-dialog-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent(text);
    if (detail) expect(alert).toHaveTextContent(detail);
    expect(alert).toHaveTextContent("rid-7");
    expect(done).not.toHaveBeenCalled();
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
  });

  it("cannot be dismissed while the grant is being saved", async () => {
    const user = userEvent.setup();
    let release: (response: Response) => void = () => undefined;
    api.answer = () => new Promise<Response>((resolve) => (release = resolve));
    renderVi(<Harness onDone={vi.fn()} preset={{ login: "octo", lockLogin: true, project: "demo", role: "reader", maxLevel: "public" }} />);
    await user.click(screen.getByRole("button", { name: "Tiếp tục" }));
    await user.click(screen.getByRole("button", { name: "Xác nhận cấp quyền" }));
    const saving = await screen.findByRole("button", { name: "Đang lưu…" });
    expect(saving).toHaveAttribute("aria-disabled", "true");
    await user.keyboard("{Escape}");
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
    release(json({ project: "demo", login: "octo", role: "reader", max_level: "public", created: false }));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
  });
});

describe("ConfirmAction", () => {
  /** Confirming starts a write that never ends, as a slow API would. */
  function Confirm({ onConfirm }: { onConfirm: () => void }) {
    const [open, setOpen] = useState(true);
    const [pending, setPending] = useState(false);
    return (
      <>
        <ConfirmAction
          open={open}
          onOpenChange={setOpen}
          title="Thu hồi token 4 của octo?"
          description={<p>Máy laptop sẽ nhận 401.</p>}
          confirmLabel="Thu hồi token"
          pendingLabel="Đang thu hồi…"
          cancelLabel="Huỷ"
          pending={pending}
          error={null}
          onConfirm={() => {
            onConfirm();
            setPending(true);
          }}
          testId="confirm"
        />
        <p data-testid="open-state">{open ? "open" : "closed"}</p>
      </>
    );
  }

  it("starts on Cancel, so Enter destroys nothing", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn();
    renderVi(<Confirm onConfirm={confirm} />);
    const dialog = screen.getByRole("alertdialog", { name: "Thu hồi token 4 của octo?" });
    expect(within(dialog).getByRole("button", { name: "Huỷ" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
  });

  it("confirms with the destructive button, and holds while pending", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn();
    renderVi(<Confirm onConfirm={confirm} />);
    await user.click(screen.getByRole("button", { name: "Thu hồi token" }));
    expect(confirm).toHaveBeenCalledOnce();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Đang thu hồi…" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
  });
});

