// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { InvitePage } from "./invite-page";
import type { InvitePreview } from "./queries";

const api = vi.hoisted(() => ({
  previews: [] as unknown[],
  previewStatus: 200,
  accept: { status: 200, body: {} as unknown },
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
        if (path === "/v1/invites/preview") {
          if (api.previewStatus !== 200) return Response.json({ error: "not_found", message: "unknown link" }, { status: api.previewStatus });
          return Response.json(api.previews.length > 1 ? api.previews.shift() : api.previews[0]);
        }
        if (path === "/v1/invites/accept") return Response.json(api.accept.body, { status: api.accept.status });
        return Response.json({ error: "not_found", message: "no such thing" }, { status: 404 });
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/invite/evn_x", useRouter: () => ({ push: vi.fn() }) }));

const TOKEN = "evn_abc";

function preview(extra: Partial<InvitePreview> = {}): InvitePreview {
  return {
    kind: "join",
    invited_by: "octo",
    status: "open",
    expires_at: "2026-11-01T00:00:00Z",
    bound: false,
    viewer: null,
    for_viewer: false,
    accepted_by_viewer: false,
    project: "acme-tools",
    role: "writer",
    max_level: "internal",
    ...extra,
  };
}

function setup(previews: InvitePreview[], { loginReady = true } = {}) {
  api.previews = previews;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <InvitePage
        token={TOKEN}
        origin="http://server.test"
        hubName="Acme hub"
        logoUrl={null}
        loginReady={loginReady}
        initialError={null}
        pairing={<button type="button">pair</button>}
      />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  api.previews = [];
  api.previewStatus = 200;
  api.accept = { status: 200, body: {} };
  api.seen = [];
});

describe("the page of an invite link", () => {
  it("names the inviter and what an unbound link gives, and signs in with GitHub back to this page", async () => {
    setup([preview()]);
    const summary = await screen.findByTestId("invite-summary");
    expect(summary).toHaveAttribute("data-state", "signed-out");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("octo mời bạn vào Acme hub");
    expect(screen.getByTestId("invite-project")).toHaveTextContent("acme-tools");
    expect(screen.getByTestId("invite-role")).toBeInTheDocument();
    expect(screen.getByTestId("invite-sign-in")).toHaveAttribute("href", "/v1/auth/web/login?next=%2Finvite%2Fevn_abc");
    expect(screen.getByTestId("invite-sign-in")).toHaveTextContent("Đăng nhập bằng GitHub");
    expect(screen.queryByTestId("invite-accept")).toBeNull();
  });

  it("keeps the project and role of a bound link from a visitor, and says when GitHub sign-in is not set up", async () => {
    setup([preview({ bound: true, project: null, role: null, max_level: null })], { loginReady: false });
    await screen.findByTestId("invite-summary");
    expect(screen.getByTestId("invite-hidden")).toHaveTextContent("Dự án và vai trò sẽ hiện khi đúng tên đăng nhập đó đăng nhập");
    expect(screen.queryByTestId("invite-project")).toBeNull();
    expect(screen.queryByTestId("invite-sign-in")).toBeNull();
    expect(screen.getByTestId("invite-login-unavailable")).toHaveTextContent("báo cho octo");
  });

  it("refuses a login the link is not for, with a way to sign out", async () => {
    setup([preview({ bound: true, viewer: "hubot", project: null, role: null, max_level: null })]);
    const summary = await screen.findByTestId("invite-summary");
    expect(summary).toHaveAttribute("data-state", "other-login");
    expect(screen.getByTestId("invite-not-for-you")).toHaveTextContent("không phải hubot");
    expect(screen.getByTestId("invite-sign-out")).toHaveTextContent("Đăng xuất");
    expect(screen.queryByTestId("invite-accept")).toBeNull();
  });

  it("accepts with one button, then lists the commands with this hub's URL", async () => {
    const accepted = preview({ viewer: "alice", for_viewer: true, accepted_by_viewer: true, status: "accepted" });
    const user = setup([preview({ viewer: "alice", for_viewer: true }), accepted]);
    await user.click(await screen.findByTestId("invite-accept"));
    const list = await screen.findByTestId("invite-get-started");
    expect(api.seen).toContain("POST /v1/invites/accept");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Bạn đã nhận lời mời");
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
    expect(screen.getByTestId("invite-accepted")).toHaveTextContent("Giờ bạn làm việc trong dự án acme-tools");
    const command = (id: string) => within(list).getByTestId(id).querySelector("code")?.textContent;
    expect(command("invite-command-install")).toBe("uv tool install 'evo-ak[graphify,worker]'");
    expect(command("invite-command-login")).toBe(`evo-agents hub login --url ${window.location.origin}`);
    expect(command("invite-command-claude-3")).toBe("claude plugin install evo-kg@evo-agents");
    expect(command("invite-command-service")).toBe("evo-agents worker service install");
    expect(within(list).queryByTestId("invite-step-project")).toBeNull();
    expect(within(list).getByTestId("invite-credentials")).toHaveTextContent("Claude Code, Codex hoặc opencode");
    expect(within(list).getByRole("button", { name: "pair" })).toBeInTheDocument();
    expect(within(list).getByTestId("invite-run-plans")).toHaveAttribute("href", "/p/acme-tools/plans");
    for (const group of within(list).getAllByRole("group")) expect(within(group).getByRole("button")).toBeInTheDocument();
  });

  it("gives a register link the harness and registration, with the project name typed in", async () => {
    const user = setup([
      preview({ kind: "register", project: null, role: null, max_level: null, viewer: "alice", status: "accepted", accepted_by_viewer: true }),
    ]);
    const list = await screen.findByTestId("invite-get-started");
    expect(screen.getByTestId("invite-accepted")).toHaveAttribute("data-kind", "register");
    const command = (id: string) => within(list).getByTestId(id).querySelector("code")?.textContent;
    expect(command("invite-command-harness")).toBe("evo-agents harness init");
    expect(command("invite-command-register")).toBe("evo-agents hub project register");
    await user.type(within(list).getByTestId("invite-project-name"), "Acme Tools");
    expect(within(list).getByTestId("invite-project-name")).toHaveAttribute("aria-invalid", "true");
    expect(command("invite-command-harness")).toBe("evo-agents harness init");
    await user.clear(within(list).getByTestId("invite-project-name"));
    await user.type(within(list).getByTestId("invite-project-name"), "acme-tools");
    expect(command("invite-command-harness")).toBe("evo-agents harness init --project acme-tools");
    expect(within(list).getByTestId("invite-step-worker")).toHaveTextContent("Ghép worker sau khi đăng ký dự án");
    expect(within(list).getByTestId("invite-open-home")).toHaveAttribute("href", "/");
  });

  it("ends a reader's list with the project to read", async () => {
    setup([preview({ role: "reader", viewer: "alice", status: "accepted", accepted_by_viewer: true })]);
    const list = await screen.findByTestId("invite-get-started");
    expect(within(list).getByTestId("invite-open-project")).toHaveAttribute("href", "/p/acme-tools");
    expect(within(list).queryByTestId("invite-step-worker")).toBeNull();
    expect(within(list).queryByTestId("invite-step-run")).toBeNull();
  });

  it("says a link was revoked", async () => {
    setup([preview({ status: "revoked", project: null, role: null, max_level: null })]);
    expect(await screen.findByTestId("invite-closed")).toHaveTextContent("Link mời này đã bị thu hồi");
  });

  it("says an unknown link is not valid", async () => {
    api.previewStatus = 404;
    setup([]);
    expect(await screen.findByTestId("invite-unknown")).toHaveTextContent("Link mời này không hợp lệ");
  });

  it("shows why an accept failed, without leaving the link", async () => {
    api.accept = { status: 410, body: { error: "gone", message: "this invite link was revoked" } };
    const user = setup([preview({ viewer: "alice", for_viewer: true })]);
    await user.click(await screen.findByTestId("invite-accept"));
    await waitFor(() => expect(screen.getByTestId("invite-summary")).toHaveTextContent("Hãy xin octo một link mới"));
  });
});
