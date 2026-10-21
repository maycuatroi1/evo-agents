// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { LoginDialog } from "./login-dialog";
import { defaultExpiryDay } from "./login-model";
import type { Account } from "./queries";

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

const worker = (name: string, owner: string) => ({
  id: name.length,
  name,
  owner,
  status: "online",
  hostname: `${name}.local`,
  os: "macOS",
  arch: "arm64",
  agent_version: "0.15.1",
  slots: 1,
  labels: [],
  projects: ["demo"],
  runtimes: {},
  checkouts: {},
  allow_web_terminal: false,
  dispatch_from: "any",
  held_runs: 0,
  created_at: "2026-10-01T00:00:00Z",
  last_heartbeat_at: null,
  drained_at: null,
  revoked_at: null,
});

const ACCOUNT: Account = {
  name: "claude-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  provider: "claude",
  category: "model",
  auth: "token",
  projects: ["demo", "docs"],
  workers: ["mac-mini"],
  weight: 3,
  priority: 2,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: "2026-10-30T00:00:00Z",
  state: "ready",
  last_usage: null,
  last_usage_at: null,
  runs_24h: 0,
  runs_7d: 4,
  cost_24h_usd: 0,
  cost_7d_usd: 1.5,
  held_by: [],
};

const TOKEN = "sk-ant-oat01-unit-token-7f3a";

const written = (name: string, created: boolean) => ({
  name,
  kind: "env",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  url_prefix: null,
  username: null,
  projects: ["demo"],
  workers: [],
  expires_at: "2027-10-13T00:00:00Z",
  created_at: "2026-10-14T00:00:00Z",
  updated_at: "2026-10-14T00:00:00Z",
  created,
});

function reads(request: Request): Response | null {
  const url = new URL(request.url);
  if (url.pathname === "/v1/auth/whoami") return json({ login: "octo", admin: false, grants: [] });
  if (url.pathname === "/v1/projects") return json([project("demo", "writer"), project("docs", "reader"), project("ops", "admin")]);
  if (url.pathname === "/v1/workers") return json([worker("mac-mini", "octo"), worker("lab-box", "someone-else")]);
  if (url.pathname === "/v1/secrets") {
    return json([{ ...written("gitlab-docs", false), kind: "git", env_var: null, url_prefix: "https://gitlab.example.org/g" }]);
  }
  return null;
}

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

function Harness({ account = null }: { account?: Account | null }) {
  const [open, setOpen] = useState(true);
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <QueryClientProvider client={client}>
      <LoginDialog open={open} onOpenChange={setOpen} account={account} accounts={[ACCOUNT]} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

const tokenField = () => screen.getByLabelText("Token") as HTMLInputElement;
const puts = () => api.seen.filter((request) => request.method === "PUT");

describe("LoginDialog", () => {
  it("tells an OAuth account's renewal to be the command of a terminal and asks the hub for nothing", async () => {
    api.answer = (request) => reads(request) ?? json({}, 500);
    renderVi(<Harness account={{ ...ACCOUNT, auth: "oauth", state: "needs-relogin" }} />);
    const dialog = await screen.findByTestId("login-dialog");
    expect(within(dialog).getByTestId("login-command-text").textContent).toBe("evo-agents hub account oauth-login claude-a");
    expect(within(dialog).queryByTestId("login-form")).toBeNull();
    expect(within(dialog).queryByTestId("login-token")).toBeNull();
    expect(api.seen.filter((request) => request.method !== "GET")).toEqual([]);
  });

  it("adds a Claude account once the form's checks pass, and keeps none of the token", async () => {
    const user = userEvent.setup();
    api.answer = (request) => reads(request) ?? (request.method === "PUT" ? json(written("claude-work", true)) : json({ error: "not_found" }, 404));
    renderVi(<Harness />);

    const dialog = screen.getByRole("dialog", { name: "Thêm tài khoản Claude" });
    const projects = await within(dialog).findByTestId("login-projects");
    expect(within(projects).getAllByRole("checkbox").map((box) => (box as HTMLInputElement).value)).toEqual(["demo", "ops"]);
    const workers = await within(dialog).findByTestId("login-workers");
    expect(within(workers).getAllByRole("checkbox").map((box) => (box as HTMLInputElement).value)).toEqual(["mac-mini"]);
    expect(within(dialog).getByLabelText("Token hết hạn vào ngày")).toHaveValue(defaultExpiryDay(Date.now()));
    expect(tokenField()).toHaveAttribute("type", "password");
    expect(tokenField()).not.toHaveAttribute("name");
    expect(within(dialog).getByTestId("login-steps")).toHaveTextContent("claude setup-token");
    expect(within(dialog).getByTestId("login-command-text")).toHaveTextContent("evo-agents hub account login <name> --project <project>");
    expect(within(dialog).getByTestId("login-command-copy")).toBeDisabled();

    const add = within(dialog).getByRole("button", { name: "Thêm tài khoản" });
    await user.click(add);
    expect(screen.getByText("Đặt tên cho tài khoản.")).toBeInTheDocument();
    expect(screen.getByText("Chọn ít nhất một dự án.")).toBeInTheDocument();
    expect(screen.getByText("Dán token mà claude setup-token đã in ra.")).toBeInTheDocument();
    expect(screen.getByLabelText("Tên")).toHaveFocus();

    // A git secret's name is refused; an account's name says its token will be replaced.
    await user.type(screen.getByLabelText("Tên"), "gitlab-docs");
    await user.click(add);
    expect(screen.getByTestId("login-name-error")).toHaveTextContent("không phải tài khoản Claude");
    await user.clear(screen.getByLabelText("Tên"));
    await user.type(screen.getByLabelText("Tên"), "claude-a");
    await waitFor(() => expect(screen.getByTestId("login-name-taken")).toHaveTextContent("Bạn đã có tài khoản tên claude-a"));

    await user.clear(screen.getByLabelText("Tên"));
    await user.type(screen.getByLabelText("Tên"), "claude-work");
    expect(screen.queryByTestId("login-name-taken")).toBeNull();
    await user.click(within(projects).getByRole("checkbox", { name: "demo" }));
    await user.type(tokenField(), "sk-ant-api03-console-key");
    await user.click(add);
    expect(screen.getByTestId("login-token-error")).toHaveTextContent("ANTHROPIC_API_KEY");
    expect(puts()).toHaveLength(0); // nothing sent while the form is wrong

    await user.clear(tokenField());
    await user.type(tokenField(), TOKEN);
    expect(within(dialog).getByTestId("login-command-text")).toHaveTextContent("evo-agents hub account login claude-work --project demo");
    expect(within(dialog).getByTestId("login-command-text")).not.toHaveTextContent(TOKEN);
    await user.click(add);

    expect(await screen.findByText("Đã thêm tài khoản claude-work")).toBeInTheDocument();
    const [put] = puts();
    expect(new URL(put.url).pathname).toBe("/v1/secrets/claude-work");
    expect(put.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await put.clone().json()).toEqual({
      kind: "env",
      env_var: "CLAUDE_CODE_OAUTH_TOKEN",
      projects: ["demo"],
      workers: [],
      expires_at: `${defaultExpiryDay(Date.now())}T00:00:00.000Z`,
      value: TOKEN,
    });
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("turns an Add under an account's name into its renewal on request", async () => {
    const user = userEvent.setup();
    api.answer = (request) => reads(request) ?? json({ error: "not_found" }, 404);
    renderVi(<Harness />);

    await user.type(screen.getByLabelText("Tên"), "claude-a");
    await user.click(await screen.findByRole("button", { name: "Gia hạn claude-a thay vào đó" }));
    const dialog = screen.getByRole("dialog", { name: "Gia hạn token của claude-a" });
    expect(within(dialog).getByTestId("login-form")).toHaveAttribute("data-mode", "renew");
    expect(within(dialog).getByLabelText("Tên")).toHaveAttribute("readonly");
    expect(within(dialog).getByTestId("login-kept-projects")).toHaveTextContent("demo, docs");
    expect(within(dialog).queryByTestId("login-projects")).toBeNull();
  });

  it("renews a token with the account's bindings, says why the hub refused, and forgets the token", async () => {
    const user = userEvent.setup();
    let refuse = true;
    api.answer = (request) =>
      reads(request) ??
      (request.method === "PUT"
        ? refuse
          ? json({ error: "invalid", message: "expires_at is past already: a secret's end is in the future" }, 422)
          : json(written("claude-a", false))
        : json({ error: "not_found" }, 404));
    renderVi(<Harness account={ACCOUNT} />);

    const dialog = screen.getByRole("dialog", { name: "Gia hạn token của claude-a" });
    expect(within(dialog).getByLabelText("Tên")).toHaveValue("claude-a");
    expect(within(dialog).getByLabelText("Tên")).toHaveAttribute("readonly");
    expect(within(dialog).getByTestId("login-kept-projects")).toHaveTextContent("demo, docs");
    expect(within(dialog).getByTestId("login-kept-workers")).toHaveTextContent("mac-mini");
    expect(within(dialog).getByTestId("login-command-text")).toHaveTextContent(/^evo-agents hub account login claude-a$/);

    await user.type(tokenField(), TOKEN);
    await user.click(within(dialog).getByRole("button", { name: "Gia hạn token" }));
    const alert = await screen.findByTestId("account-dialog-error");
    expect(alert).toHaveTextContent("Hub từ chối token hoặc ngày hết hạn của nó.");
    expect(alert).toHaveTextContent("expires_at is past already");
    expect(alert).toHaveTextContent("rid-9");
    expect(screen.getByTestId("login-token-forgotten")).toBeInTheDocument();
    expect(tokenField()).toHaveValue("");
    expect(await puts()[0].clone().json()).toEqual({
      kind: "env",
      env_var: "CLAUDE_CODE_OAUTH_TOKEN",
      projects: ["demo", "docs"],
      workers: ["mac-mini"],
      expires_at: `${defaultExpiryDay(Date.now())}T00:00:00.000Z`,
      value: TOKEN,
    });
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");

    refuse = false;
    await user.type(tokenField(), TOKEN);
    await user.click(within(dialog).getByRole("button", { name: "Gia hạn token" }));
    expect(await screen.findByText("Đã gia hạn token của claude-a")).toBeInTheDocument();
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
    expect(document.body.innerHTML).not.toContain(TOKEN);
    // A renewal reads no project, worker or secret list: what it keeps is the account's own.
    expect(api.seen.some((request) => ["/v1/projects", "/v1/workers", "/v1/secrets"].includes(new URL(request.url).pathname))).toBe(false);
  });

  it("explains that a member without the writer role cannot add an account", async () => {
    api.answer = (request) => {
      if (new URL(request.url).pathname === "/v1/projects") return json([project("docs", "reader")]);
      return reads(request) ?? json({ error: "not_found" }, 404);
    };
    renderVi(<Harness />);
    expect(await screen.findByTestId("login-no-projects")).toHaveTextContent("vai trò writer");
    expect(screen.getByRole("button", { name: "Thêm tài khoản" })).toBeDisabled();
  });
});
