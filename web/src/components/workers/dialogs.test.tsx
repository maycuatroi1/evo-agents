// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Pause } from "lucide-react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Notice } from "@/components/admin/notice";
import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { ConfirmByName } from "./confirm-by-name";
import { RegisterDialog } from "./register-dialog";

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

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-9" } });

const project = (name: string, role: string | null) => ({
  name,
  harness: null,
  levels: ["internal"],
  locations: ["any"],
  default_label: { level: "internal" },
  sinks: [],
  repos: [],
  role,
  max_level: "internal",
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
});

const PAIRING = {
  id: 5,
  code: "K7QM-4XPD",
  expires_at: new Date(Date.now() + 10 * 60_000).toISOString(),
  name: "lab-ws-01",
  projects: ["demo"],
  slots: 2,
  labels: ["gpu"],
  allow_web_terminal: false,
};

const pairingState = (status: string, extra: Record<string, unknown> = {}) => ({
  ...PAIRING,
  status,
  tries_left: 5,
  created_at: new Date().toISOString(),
  used_at: null,
  worker_id: null,
  ...extra,
});

const WORKER = {
  id: 9,
  name: "lab-ws-01",
  owner: "octo",
  status: "offline",
  hostname: "lab-ws-01.local",
  os: "Linux",
  arch: "x86_64",
  agent_version: "0.3.0",
  slots: 2,
  labels: ["gpu"],
  projects: ["demo"],
  runtimes: {},
  checkouts: {},
  allow_web_terminal: false,
  held_runs: 0,
  created_at: new Date().toISOString(),
  last_heartbeat_at: null,
  drained_at: null,
  revoked_at: null,
};

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

function Harness({ onJoined }: { onJoined: (notice: Notice) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RegisterDialog open={open} onOpenChange={setOpen} onJoined={onJoined} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

describe("RegisterDialog", () => {
  it("checks the form, creates a code with the CSRF header, and follows the pairing until the machine joins", async () => {
    const user = userEvent.setup();
    const joined = vi.fn();
    let machineJoined = false;
    api.answer = async (request) => {
      const url = new URL(request.url);
      if (url.pathname === "/v1/projects") return json([project("demo", "writer"), project("docs", "reader"), project("ops", "admin")]);
      if (url.pathname === "/v1/workers/pairings" && request.method === "POST") return json(PAIRING, 201);
      if (url.pathname === "/v1/workers/pairings/5") {
        return json(machineJoined ? pairingState("joined", { used_at: new Date().toISOString(), worker_id: 9 }) : pairingState("waiting"));
      }
      if (url.pathname === "/v1/workers/9") return json(WORKER);
      return json({ error: "not_found", message: url.pathname }, 404);
    };
    renderVi(<Harness onJoined={joined} />);

    const dialog = screen.getByRole("dialog", { name: "Đăng ký worker" });
    // Only the projects with the writer role (or project admin) are offered.
    const choices = await within(dialog).findByTestId("register-projects");
    expect(within(choices).getAllByRole("checkbox").map((box) => (box as HTMLInputElement).value)).toEqual(["demo", "ops"]);

    await user.click(within(dialog).getByRole("button", { name: "Tạo pairing code" }));
    expect(screen.getByText("Nhập tên cho worker.")).toBeInTheDocument();
    expect(screen.getByText("Chọn ít nhất một dự án.")).toBeInTheDocument();
    expect(screen.getByLabelText("Tên worker")).toHaveFocus();

    await user.type(screen.getByLabelText("Tên worker"), "lab-ws-01");
    await user.click(within(choices).getByRole("checkbox", { name: "demo" }));
    await user.clear(screen.getByLabelText("Slot"));
    await user.type(screen.getByLabelText("Slot"), "9");
    await user.type(screen.getByLabelText("Label"), "gpu, x/y");
    await user.click(within(dialog).getByRole("button", { name: "Tạo pairing code" }));
    expect(screen.getByText("Nhập một số nguyên từ 1 tới 8.")).toBeInTheDocument();
    expect(screen.getByText(/Label không hợp lệ: x\/y/)).toBeInTheDocument();
    expect(api.seen.some((request) => request.method === "POST")).toBe(false); // nothing sent while the form is wrong

    await user.clear(screen.getByLabelText("Slot"));
    await user.type(screen.getByLabelText("Slot"), "2");
    await user.clear(screen.getByLabelText("Label"));
    await user.type(screen.getByLabelText("Label"), "gpu");
    await user.click(within(dialog).getByRole("button", { name: "Tạo pairing code" }));

    expect(await screen.findByTestId("pairing-code")).toHaveTextContent("K7QM-4XPD");
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post?.clone().json()).toEqual({
      name: "lab-ws-01",
      projects: ["demo"],
      slots: 2,
      labels: ["gpu"],
      allow_web_terminal: false,
    });
    expect(screen.getByTestId("pairing-expires")).toHaveTextContent(/Hết hạn sau (10:00|9:5\d)/);
    expect(screen.getByTestId("pairing-join-command")).toHaveTextContent(
      `evo-agents worker join --url ${window.location.origin} --code K7QM-4XPD`,
    );
    expect(screen.getByTestId("register-warning")).toHaveTextContent("toàn quyền");
    expect(screen.getByTestId("pairing-waiting")).toBeInTheDocument();

    machineJoined = true; // the next poll, 2 s later, finds the worker
    expect(await screen.findByTestId("pairing-joined", undefined, { timeout: 5_000 })).toHaveTextContent("lab-ws-01");
    expect(await screen.findByTestId("pairing-joined-facts")).toHaveTextContent("lab-ws-01.local, Linux x86_64");
    expect(screen.getByRole("link", { name: "Mở worker" })).toHaveAttribute("href", "/workers/9");
    expect(joined).toHaveBeenCalledWith({ tone: "success", text: "Worker lab-ws-01 đã kết nối với hub." });
  });

  it("shows the CLI commands built from the form, and says when the hub cannot make codes", async () => {
    const user = userEvent.setup();
    api.answer = (request) => {
      const url = new URL(request.url);
      if (url.pathname === "/v1/projects") return json([project("demo", "writer")]);
      return json({ error: "service_unavailable", message: "pairing codes need EVO_HUB_SESSION_SECRET" }, 503);
    };
    renderVi(<Harness onJoined={vi.fn()} />);
    await screen.findByTestId("register-projects");
    // The only writable project is chosen already.
    expect(screen.getByRole("checkbox", { name: "demo" })).toBeChecked();
    await user.type(screen.getByLabelText("Tên worker"), "mac-mini");
    await user.click(screen.getByRole("tab", { name: "Chỉ dùng CLI" }));
    expect(screen.getByTestId("register-cli-command")).toHaveTextContent(
      "evo-agents worker register --name mac-mini --project demo --slots 1",
    );
    await user.click(screen.getByRole("tab", { name: "Pairing code" }));
    await user.click(screen.getByRole("button", { name: "Tạo pairing code" }));
    const alert = await screen.findByTestId("admin-dialog-error");
    expect(alert).toHaveTextContent("Hub hiện không tạo được pairing code.");
    expect(alert).toHaveTextContent("EVO_HUB_SESSION_SECRET");
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
  });

  it("explains that a member without the writer role cannot register a worker", async () => {
    api.answer = () => json([project("docs", "reader")]);
    renderVi(<Harness onJoined={vi.fn()} />);
    expect(await screen.findByTestId("register-no-projects")).toHaveTextContent("vai trò writer");
    expect(screen.getByRole("button", { name: "Tạo pairing code" })).toBeDisabled();
  });
});

describe("ConfirmByName", () => {
  function Confirm({ onConfirm }: { onConfirm: () => void }) {
    const [open, setOpen] = useState(true);
    return (
      <>
        <ConfirmByName
          open={open}
          onOpenChange={setOpen}
          name="lab-ws-01"
          icon={Pause}
          tone="warning"
          title="Cho worker lab-ws-01 ngừng nhận việc?"
          description={<p>Worker chạy xong các run đang giữ.</p>}
          confirmLabel="Ngừng nhận việc"
          pendingLabel="Đang dừng…"
          pending={false}
          error={null}
          onConfirm={onConfirm}
          testId="drain"
        />
        <p data-testid="open-state">{open ? "open" : "closed"}</p>
      </>
    );
  }

  it("starts in the name field and confirms only once the worker's name is typed", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn();
    renderVi(<Confirm onConfirm={confirm} />);
    const field = screen.getByTestId("drain-name");
    await waitFor(() => expect(field).toHaveFocus());

    await user.click(screen.getByTestId("drain-confirm"));
    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByText("Đây không phải tên worker. Gõ đúng lab-ws-01.")).toBeInTheDocument();
    expect(field).toHaveAttribute("aria-invalid", "true");

    await user.type(field, "lab-ws-0");
    await user.keyboard("{Enter}");
    expect(confirm).not.toHaveBeenCalled();
    await user.type(field, "1{Enter}");
    expect(confirm).toHaveBeenCalledOnce();
  });

  it("closes on Cancel without confirming", async () => {
    const user = userEvent.setup();
    const confirm = vi.fn();
    renderVi(<Confirm onConfirm={confirm} />);
    await user.click(screen.getByRole("button", { name: "Huỷ" }));
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
    expect(confirm).not.toHaveBeenCalled();
  });
});
