// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import type { Worker } from "./queries";
import { UpdateBadge } from "./update-badge";
import { UpdateNotes, VersionCard } from "./version-card";
import { VersionLockSwitch } from "./version-lock";

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

const SHA = "ca3e9af0f1b2c3d4e5f60718293a4b5c6d7e8f90";
const NIGHTLY = "def5678a1b2c3d4e5f60718293a4b5c6d7e8f901";
const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-7" } });

function worker(overrides: Partial<Worker> = {}): Worker {
  return {
    id: 9,
    name: "lab-ws-01",
    owner: "octo",
    status: "online",
    hostname: "lab-ws-01.local",
    os: "Linux",
    arch: "x86_64",
    agent_version: "0.22.0",
    agent_commit: SHA,
    slots: 2,
    labels: [],
    plugins: [],
    version_locked: false,
    self_update: { state: "current", nightly_commit: SHA, checked_at: new Date().toISOString(), since: new Date().toISOString() },
    update_now: null,
    nightly: null,
    projects: ["demo"],
    runtimes: {},
    checkouts: {},
    free_slots: 2,
    allow_web_terminal: false,
    dispatch_from: "any",
    held_runs: 0,
    created_at: "2026-10-01T00:00:00Z",
    last_heartbeat_at: new Date().toISOString(),
    drained_at: null,
    revoked_at: null,
    ...overrides,
  };
}

function renderWithQueries(ui: ReactElement) {
  return renderVi(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("VersionLockSwitch", () => {
  it("locks with a PATCH that carries the CSRF header, and says so in a toast", async () => {
    const user = userEvent.setup();
    api.answer = () => json(worker({ version_locked: true }));
    renderWithQueries(<VersionLockSwitch worker={worker()} />);

    const toggle = screen.getByRole("switch", { name: "Khoá phiên bản" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/Bật: worker đứng yên/);
    await user.click(toggle);

    expect(await screen.findByText("lab-ws-01 đứng yên ở bản hiện tại")).toBeInTheDocument();
    const patch = api.seen.find((request) => request.method === "PATCH");
    expect(new URL(patch?.url ?? "").pathname).toBe("/v1/workers/9");
    expect(patch?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await patch?.clone().json()).toEqual({ version_locked: true });
    expect(screen.queryByTestId("worker-version-lock-error")).toBeNull();
  });

  it("keeps what the hub refused under the switch, with its message and request id", async () => {
    const user = userEvent.setup();
    api.answer = () => json({ error: "service_unavailable", message: "database is read-only" }, 503);
    renderWithQueries(<VersionLockSwitch worker={worker({ version_locked: true })} />);

    const toggle = screen.getByRole("switch", { name: "Khoá phiên bản" });
    await user.click(toggle);

    const alert = await screen.findByTestId("worker-version-lock-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("Không mở khoá được phiên bản của lab-ws-01");
    expect(alert).toHaveTextContent("database is read-only");
    expect(alert).toHaveTextContent("rid-7");
    expect(toggle).toBeChecked(); // it shows what the hub holds
    expect(toggle.getAttribute("aria-describedby")).toContain(alert.parentElement?.id ?? "missing");
  });

  it("says a 409 in a toast, since the page reloads to the revoked worker", async () => {
    const user = userEvent.setup();
    api.answer = () => json({ error: "conflict", message: "worker 9 is revoked" }, 409);
    renderWithQueries(<VersionLockSwitch worker={worker()} />);
    await user.click(screen.getByRole("switch", { name: "Khoá phiên bản" }));
    expect(await screen.findByText("Không khoá được phiên bản của lab-ws-01")).toBeInTheDocument();
    expect(screen.queryByTestId("worker-version-lock-error")).toBeNull();
  });
});

describe("VersionCard", () => {
  it("shows the release and links the commit to GitHub", () => {
    renderVi(<VersionCard worker={worker()} isOwner />);
    const card = screen.getByTestId("worker-version");
    expect(within(card).getByTestId("worker-agent-version")).toHaveTextContent("0.22.0");
    const link = within(card).getByRole("link", { name: "ca3e9af trên GitHub (mở trong tab mới)" });
    expect(link).toHaveAttribute("href", `https://github.com/maycuatroi1/evo-agents/commit/${SHA}`);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(card).getByTestId("worker-updates")).toHaveAttribute("data-state", "current");
  });

  it("says when no commit was reported, and who locked the version", () => {
    renderVi(<VersionCard worker={worker({ agent_commit: null, self_update: null, version_locked: true })} isOwner={false} />);
    expect(screen.getByTestId("worker-commit")).toHaveTextContent("Chưa báo");
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByTestId("worker-updates")).toHaveTextContent("octo đã khoá");
  });

  it("names the commit a waiting daemon will install", () => {
    const waiting = worker({ self_update: { state: "waiting", nightly_commit: NIGHTLY, since: new Date().toISOString() } });
    renderWithQueries(<VersionCard worker={waiting} isOwner />);
    const updates = screen.getByTestId("worker-updates");
    expect(updates).toHaveAttribute("data-state", "waiting");
    expect(within(updates).getByRole("link", { name: "def5678 trên GitHub (mở trong tab mới)" })).toBeInTheDocument();
  });
});

describe("VersionCard's nightly", () => {
  it("shows the commit the hub read, and says nothing more while that read is fresh", () => {
    renderVi(<VersionCard worker={worker({ nightly: { commit: NIGHTLY, read_at: new Date().toISOString() } })} isOwner={false} />);
    const row = screen.getByTestId("worker-hub-nightly");
    expect(within(row).getByRole("link", { name: "def5678 trên GitHub (mở trong tab mới)" })).toBeInTheDocument();
    expect(screen.queryByTestId("worker-hub-nightly-stale")).toBeNull();
  });

  it("says when the hub's read is over 10 minutes old, and when the hub has not read it yet", () => {
    const old = new Date(Date.now() - 25 * 60 * 1000).toISOString();
    const { unmount } = renderVi(<VersionCard worker={worker({ nightly: { commit: NIGHTLY, read_at: old } })} isOwner={false} />);
    expect(screen.getByTestId("worker-hub-nightly-stale")).toHaveTextContent("hub chưa tới được GitHub");
    unmount();
    renderVi(<VersionCard worker={worker()} isOwner={false} />);
    expect(screen.getByTestId("worker-hub-nightly")).toHaveTextContent("Hub chưa đọc lần nào.");
  });
});

describe("UpdateNow", () => {
  const behind = (overrides: Partial<Worker> = {}) =>
    worker({ nightly: { commit: NIGHTLY, read_at: new Date().toISOString() }, features: ["update_now"], ...overrides });
  const open = { requested_at: new Date().toISOString(), ended_at: null, outcome: null, reason: null };

  it("shows nothing while the worker runs the commit the hub read", () => {
    renderWithQueries(<VersionCard worker={behind({ agent_commit: NIGHTLY })} isOwner />);
    expect(screen.queryByTestId("update-now")).toBeNull();
  });

  it("asks with a POST that carries the CSRF header, and says so in a toast", async () => {
    const user = userEvent.setup();
    api.answer = () => json(behind({ update_now: open }));
    renderWithQueries(<VersionCard worker={behind()} isOwner />);

    const button = screen.getByRole("button", { name: "Cập nhật ngay" });
    expect(button).toHaveAccessibleDescription(/chạy commit khác nightly/);
    await user.click(button);

    expect(await screen.findByText("lab-ws-01 sẽ cập nhật ngay")).toBeInTheDocument();
    const post = api.seen.find((request) => request.method === "POST");
    expect(new URL(post?.url ?? "").pathname).toBe("/v1/workers/9/update-now");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
  });

  it("greys the button with why when the daemon is too old, and sends nothing", async () => {
    const user = userEvent.setup();
    renderWithQueries(<VersionCard worker={behind({ features: [] })} isOwner />);
    const button = screen.getByRole("button", { name: "Cập nhật ngay" });
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).toHaveAccessibleDescription(/Daemon quá cũ/);
    await user.click(button);
    expect(api.seen).toEqual([]);
  });

  it("says what an open request waits for, and cancels it", async () => {
    const user = userEvent.setup();
    api.answer = () => json(behind({ update_now: { ...open, ended_at: new Date().toISOString(), outcome: "cancelled" } }));
    renderWithQueries(<VersionCard worker={behind({ update_now: open, held_runs: 2 })} isOwner />);

    const status = screen.getByTestId("update-now-status");
    expect(status).toHaveAttribute("data-phase", "runs");
    expect(status).toHaveTextContent("chờ 2 run đang giữ chạy xong");
    await user.click(screen.getByRole("button", { name: "Huỷ" }));

    expect(await screen.findByText("Đã huỷ Cập nhật ngay của lab-ws-01")).toBeInTheDocument();
    const post = api.seen.find((request) => request.method === "POST");
    expect(new URL(post?.url ?? "").pathname).toBe("/v1/workers/9/update-now/cancel");
  });

  it("lets a hub admin who is not the owner read an open request, and nothing else", () => {
    const { unmount } = renderVi(<VersionCard worker={behind({ update_now: open })} isOwner={false} />);
    expect(screen.getByTestId("update-now-status")).toHaveAttribute("data-phase", "asked");
    expect(screen.queryByRole("button", { name: "Huỷ" })).toBeNull();
    unmount();
    renderVi(<VersionCard worker={behind()} isOwner={false} />);
    expect(screen.queryByTestId("update-now")).toBeNull();
  });

  it("keeps a 503 under the button, with its message and request id", async () => {
    const user = userEvent.setup();
    api.answer = () => json({ error: "service_unavailable", message: "database is read-only" }, 503);
    renderWithQueries(<VersionCard worker={behind()} isOwner />);
    const button = screen.getByRole("button", { name: "Cập nhật ngay" });
    await user.click(button);

    const alert = await screen.findByTestId("update-now-error");
    expect(alert).toHaveTextContent("Không yêu cầu lab-ws-01 cập nhật ngay được");
    expect(alert).toHaveTextContent("database is read-only");
    expect(alert).toHaveTextContent("rid-7");
    expect(button.getAttribute("aria-describedby")).toContain(alert.parentElement?.id ?? "missing");
  });

  it("says a 409 in a toast with the hub's reason", async () => {
    const user = userEvent.setup();
    api.answer = () => json({ error: "conflict", message: "worker 9 runs def5678a1b2c already, the commit nightly points to" }, 409);
    renderWithQueries(<VersionCard worker={behind()} isOwner />);
    await user.click(screen.getByRole("button", { name: "Cập nhật ngay" }));
    expect(await screen.findByText("Không yêu cầu lab-ws-01 cập nhật ngay được")).toBeInTheDocument();
    expect(await screen.findByText(/runs def5678a1b2c already/)).toBeInTheDocument();
    expect(screen.queryByTestId("update-now-error")).toBeNull();
  });
});

describe("UpdateNotes and UpdateBadge", () => {
  it("say nothing while updates go as they should", () => {
    renderVi(
      <>
        <UpdateNotes worker={worker()} isOwner />
        <UpdateBadge worker={worker()} />
      </>,
    );
    expect(screen.queryByTestId(/^worker-update/)).toBeNull();
  });

  it("show an update that failed, with the daemon's own error", () => {
    const failed = worker({
      self_update: { state: "waiting", nightly_commit: NIGHTLY, error: "uv tool install: exit status 2", error_at: new Date().toISOString(), since: new Date().toISOString() },
    });
    renderVi(
      <>
        <UpdateNotes worker={failed} isOwner />
        <UpdateBadge worker={failed} />
      </>,
    );
    expect(screen.getByTestId("worker-update-failed")).toHaveTextContent("Lần cập nhật lên nightly gần nhất bị lỗi lúc");
    expect(screen.getByTestId("worker-update-error")).toHaveTextContent("uv tool install: exit status 2");
    expect(screen.getByTestId("worker-update")).toHaveAttribute("data-problem", "failed");
    expect(screen.getByTestId("worker-update")).toHaveTextContent("Cập nhật lỗi");
  });

  it("show why a daemon cannot update itself, and an icon alone on a phone's row", () => {
    const unsupported = worker({ self_update: { state: "unsupported", reason: "editable install of a local checkout", since: null } });
    renderVi(
      <>
        <UpdateNotes worker={unsupported} isOwner />
        <UpdateBadge worker={unsupported} compact />
      </>,
    );
    expect(screen.getByTestId("worker-update-reason")).toHaveTextContent("editable install of a local checkout");
    const icon = screen.getByTestId("worker-update");
    expect(icon).toHaveAttribute("data-problem", "unsupported");
    expect(icon).toHaveTextContent("Không tự cập nhật được"); // its words, for screen readers
  });

  it("show a wait of over 12 hours, and tell the owner how to let it update", () => {
    const since = new Date(Date.now() - 13 * 60 * 60 * 1000).toISOString();
    const stalled = worker({ self_update: { state: "waiting", nightly_commit: NIGHTLY, since } });
    renderVi(<UpdateNotes worker={stalled} isOwner />);
    expect(screen.getByTestId("worker-update-waiting")).toHaveTextContent("cho worker ngừng nhận việc");
  });
});
