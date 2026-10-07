// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Notice } from "@/components/feedback/toast";
import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import type { Secret } from "./queries";
import { SecretDialog } from "./secret-dialog";

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

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-7" } });

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

const worker = (name: string, owner: string, status = "online") => ({
  id: name.length,
  name,
  owner,
  status,
  hostname: `${name}.local`,
  os: "macOS",
  arch: "arm64",
  agent_version: "0.4.0",
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
  revoked_at: status === "revoked" ? "2026-10-02T00:00:00Z" : null,
});

const SECRET: Secret = {
  name: "gitlab-docs",
  kind: "git",
  env_var: null,
  url_prefix: "https://gitlab.example.org/group",
  username: "oauth2",
  projects: ["demo"],
  workers: ["mac-mini"],
  expires_at: null,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-02T00:00:00Z",
};

const VALUE = "e2e-unit-value-4f1c";

function reads(request: Request): Response | null {
  const url = new URL(request.url);
  if (url.pathname === "/v1/auth/whoami") return json({ login: "octo", admin: false, grants: [] });
  if (url.pathname === "/v1/projects") return json([project("demo", "writer"), project("docs", "reader"), project("ops", "admin")]);
  if (url.pathname === "/v1/workers") {
    return json([worker("mac-mini", "octo"), worker("lab-box", "someone-else"), worker("old-one", "octo", "revoked")]);
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

function Harness({ secret = null, taken = [], onSaved }: { secret?: Secret | null; taken?: string[]; onSaved: (notice: Notice) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <SecretDialog open={open} onOpenChange={setOpen} secret={secret} taken={taken} onSaved={onSaved} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

const valueField = () => screen.getByLabelText("Value") as HTMLInputElement;

describe("SecretDialog", () => {
  it("checks the form, sends the value once with the CSRF header, and keeps none of it", async () => {
    const user = userEvent.setup();
    const saved = vi.fn();
    api.answer = (request) =>
      reads(request) ?? (request.method === "PUT" ? json({ ...SECRET, name: "claude-oauth", created: true }) : json({ error: "not_found", message: "?" }, 404));
    renderVi(<Harness onSaved={saved} taken={["gitlab-docs"]} />);

    const dialog = screen.getByRole("dialog", { name: "Thêm secret" });
    // Writable projects only; the visitor's own workers that are not revoked only.
    const projects = await within(dialog).findByTestId("secret-projects");
    await waitFor(() => expect(within(projects).getAllByRole("checkbox")).toHaveLength(2));
    expect(within(projects).getAllByRole("checkbox").map((box) => (box as HTMLInputElement).value)).toEqual(["demo", "ops"]);
    const workers = within(dialog).getByTestId("secret-workers");
    await waitFor(() => expect(within(workers).getAllByRole("checkbox").map((box) => (box as HTMLInputElement).value)).toEqual(["mac-mini"]));

    // The value field is a password field the browser never submits by itself.
    expect(valueField()).toHaveAttribute("type", "password");
    expect(valueField()).not.toHaveAttribute("name");
    expect(valueField()).toHaveValue("");

    await user.click(within(dialog).getByRole("button", { name: "Thêm secret" }));
    expect(screen.getByText("Đặt tên cho secret.")).toBeInTheDocument();
    expect(screen.getByText("Nhập tên biến mà secret đặt.")).toBeInTheDocument();
    expect(screen.getByText("Chọn ít nhất một dự án.")).toBeInTheDocument();
    expect(screen.getByText("Nhập hoặc dán value.")).toBeInTheDocument();
    expect(screen.getByLabelText("Tên")).toHaveFocus();

    await user.type(screen.getByLabelText("Tên"), "gitlab-docs");
    await user.type(screen.getByLabelText("Tên biến"), "PATH");
    await user.click(within(dialog).getByRole("button", { name: "Thêm secret" }));
    expect(screen.getByText(/Bạn đã có secret tên này/)).toBeInTheDocument();
    expect(screen.getByText(/Biến này điều khiển shell/)).toBeInTheDocument();
    expect(api.seen.some((request) => request.method === "PUT")).toBe(false); // nothing sent while the form is wrong

    await user.clear(screen.getByLabelText("Tên"));
    await user.type(screen.getByLabelText("Tên"), "claude-oauth");
    await user.clear(screen.getByLabelText("Tên biến"));
    await user.type(screen.getByLabelText("Tên biến"), "CLAUDE_CODE_OAUTH_TOKEN");
    await user.click(within(projects).getByRole("checkbox", { name: "demo" }));
    await user.type(valueField(), VALUE);
    await user.click(within(dialog).getByRole("button", { name: "Thêm secret" }));

    await waitFor(() =>
      expect(saved).toHaveBeenCalledWith({
        tone: "success",
        text: "Đã thêm secret claude-oauth",
        description: "Hub đã niêm phong value và sẽ không hiện lại.",
      }),
    );
    const put = api.seen.find((request) => request.method === "PUT");
    expect(new URL(put?.url ?? "").pathname).toBe("/v1/secrets/claude-oauth");
    expect(put?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await put?.clone().json()).toEqual({
      kind: "env",
      env_var: "CLAUDE_CODE_OAUTH_TOKEN",
      projects: ["demo"],
      workers: [],
      expires_at: null,
      value: VALUE,
    });
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
    expect(document.body.innerHTML).not.toContain(VALUE);
  });

  it("fills a replace with the secret as the hub holds it but its value, and forgets the value the hub refused", async () => {
    const user = userEvent.setup();
    api.answer = (request) =>
      reads(request) ??
      (request.method === "PUT"
        ? json({ error: "forbidden", message: "a secret for project demo needs the writer role on it; you hold reader" }, 403)
        : json({ error: "not_found", message: "?" }, 404));
    renderVi(<Harness secret={SECRET} onSaved={vi.fn()} />);

    const dialog = screen.getByRole("dialog", { name: "Thay secret gitlab-docs" });
    expect(within(dialog).queryByLabelText("Tên")).toBeNull(); // a replace keeps its name
    expect(within(dialog).getByRole("radio", { name: /Credential git/ })).toBeChecked();
    expect(within(dialog).getByLabelText("Tiền tố URL")).toHaveValue("https://gitlab.example.org/group");
    expect(within(dialog).getByLabelText("Username")).toHaveValue("oauth2");
    await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: "demo" })).toBeChecked());
    await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: "mac-mini" })).toBeChecked());
    expect(valueField()).toHaveValue("");

    await user.click(within(dialog).getByRole("button", { name: "Thay secret" }));
    expect(screen.getByText("Nhập hoặc dán value.")).toBeInTheDocument();
    expect(valueField()).toHaveFocus();

    await user.type(valueField(), VALUE);
    await user.click(within(dialog).getByRole("button", { name: "Thay secret" }));
    const alert = await screen.findByTestId("admin-dialog-error");
    expect(alert).toHaveTextContent("Hub từ chối");
    expect(alert).toHaveTextContent("you hold reader");
    expect(screen.getByTestId("secret-value-forgotten")).toBeInTheDocument();
    expect(valueField()).toHaveValue("");
    const put = api.seen.find((request) => request.method === "PUT");
    expect(await put?.clone().json()).toEqual({
      kind: "git",
      url_prefix: "https://gitlab.example.org/group",
      username: "oauth2",
      projects: ["demo"],
      workers: ["mac-mini"],
      expires_at: null,
      value: VALUE,
    });
    expect(document.body.innerHTML).not.toContain(VALUE);
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
  });

  it("explains that a member without the writer role cannot add a secret", async () => {
    api.answer = (request) => {
      const url = new URL(request.url);
      if (url.pathname === "/v1/projects") return json([project("docs", "reader")]);
      return reads(request) ?? json({ error: "not_found", message: "?" }, 404);
    };
    renderVi(<Harness onSaved={vi.fn()} />);
    expect(await screen.findByTestId("secret-no-projects")).toHaveTextContent("vai trò writer");
    expect(screen.getByRole("button", { name: "Thêm secret" })).toBeDisabled();
  });
});
