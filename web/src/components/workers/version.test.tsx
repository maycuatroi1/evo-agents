// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import type { Worker } from "./queries";
import { SharedSwitch } from "./shared-switch";
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
    shared: false,
    self_update: { state: "current", nightly_commit: SHA, checked_at: new Date().toISOString(), since: new Date().toISOString() },
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

describe("SharedSwitch", () => {
  it("shares the worker with a PATCH that carries the CSRF header, and says so in a toast", async () => {
    const user = userEvent.setup();
    api.answer = () => json(worker({ shared: true }));
    renderWithQueries(<SharedSwitch worker={worker()} />);

    const toggle = screen.getByRole("switch", { name: "Chia sẻ với writer của các dự án của nó" });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription(/mỗi lúc run của một member/);
    await user.click(toggle);

    expect(await screen.findByText("lab-ws-01 đã được chia sẻ")).toBeInTheDocument();
    const patch = api.seen.find((request) => request.method === "PATCH");
    expect(new URL(patch?.url ?? "").pathname).toBe("/v1/workers/9");
    expect(patch?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await patch?.clone().json()).toEqual({ shared: true });
  });

  it("says in a toast what the hub refused, and shows what it holds", async () => {
    const user = userEvent.setup();
    api.answer = () => json({ error: "forbidden", message: "only octo, who owns worker 9, may change its settings" }, 403);
    renderWithQueries(<SharedSwitch worker={worker({ shared: true })} />);

    const toggle = screen.getByRole("switch", { name: "Chia sẻ với writer của các dự án của nó" });
    await user.click(toggle);

    expect(await screen.findByText("Không đổi được chế độ chia sẻ của lab-ws-01")).toBeInTheDocument();
    expect(toggle).toBeChecked();
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
    renderVi(<VersionCard worker={waiting} isOwner />);
    const updates = screen.getByTestId("worker-updates");
    expect(updates).toHaveAttribute("data-state", "waiting");
    expect(within(updates).getByRole("link", { name: "def5678 trên GitHub (mở trong tab mới)" })).toBeInTheDocument();
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
