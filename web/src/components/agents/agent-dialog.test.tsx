// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Agent } from "@/lib/agents";
import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { AgentDialog } from "./agent-dialog";

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

const SAVED: Agent = {
  handle: "reviewer",
  project: "demo",
  name: "Reviewer",
  description: "",
  instructions: "",
  color: "#2563eb",
  icon_url: null,
  avatar_sha256: null,
  is_default: false,
  permissions: {},
  restricted: ["tool"],
  version: 1,
  created_by: "octo",
  created_at: "2026-10-14T03:00:00Z",
  updated_at: "2026-10-14T03:00:00Z",
  deleted_at: null,
};

function answer(post: (request: Request) => Response | Promise<Response>) {
  return (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/v1/secrets") return json([{ name: "npm-token", kind: "env", env_var: "NPM_TOKEN", url_prefix: null }]);
    if (url.pathname === "/v1/skills") return json([]);
    if (url.pathname === "/v1/workers") return json([]);
    if (url.pathname === "/v1/projects") return json([{ name: "demo", role: "admin" }]);
    if (url.pathname === "/v1/projects/demo/agents" && request.method === "POST") return post(request);
    return json({ error: "not_found", message: url.pathname }, 404);
  };
}

function Harness({ agent = null, onSaved }: { agent?: Agent | null; onSaved: (agent: Agent) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AgentDialog project="demo" agent={agent} open={open} onOpenChange={setOpen} onSaved={onSaved} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("AgentDialog", () => {
  it("shows the fields wrong once the admin tries to save, then creates the agent with the allowlists picked", async () => {
    const user = userEvent.setup();
    const saved = vi.fn();
    api.answer = answer(() => json(SAVED, 201));
    renderVi(<Harness onSaved={saved} />);
    const dialog = screen.getByRole("dialog", { name: "Agent mới" });

    // A project's name is refused as a handle, and nothing goes out while a field is wrong.
    await user.type(within(dialog).getByLabelText("Handle"), "demo");
    await user.click(within(dialog).getByTestId("agent-save"));
    expect(within(dialog).getByTestId("agent-handle")).toHaveTextContent("Đây là tên một dự án");
    expect(within(dialog).getByTestId("agent-name")).toHaveTextContent("Hãy điền trường này.");
    expect(api.seen.some((request) => request.method === "POST")).toBe(false);

    await user.clear(within(dialog).getByLabelText("Handle"));
    await user.type(within(dialog).getByLabelText("Handle"), "reviewer");
    await user.type(within(dialog).getByLabelText("Tên"), "Reviewer");

    // Tools: only read, picked from the list; credentials: the admin's own secret and one typed by hand.
    const tools = within(dialog).getByTestId("agent-axis-tool");
    await user.click(within(tools).getByTestId("agent-axis-tool-allowlist"));
    expect(within(tools).getByTestId("agent-axis-tool-count")).toHaveTextContent("Không cho phép gì");
    await user.click(within(tools).getByRole("checkbox", { name: /^read/ }));
    const credentials = within(dialog).getByTestId("agent-axis-credential");
    await user.click(within(credentials).getByTestId("agent-axis-credential-allowlist"));
    await user.click(await within(credentials).findByRole("checkbox", { name: /^npm-token/ }));
    await user.type(within(credentials).getByTestId("agent-axis-credential-typed"), "Bad Name{Enter}");
    expect(within(credentials).getByTestId("agent-field-error")).toHaveTextContent("Tên secret gồm chữ thường");
    await user.clear(within(credentials).getByTestId("agent-axis-credential-typed"));
    await user.type(within(credentials).getByTestId("agent-axis-credential-typed"), "pypi-token{Enter}");
    expect(within(credentials).getByRole("checkbox", { name: /^pypi-token/ })).toBeChecked();

    await user.click(within(dialog).getByTestId("agent-save"));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    const body = await post?.clone().json();
    expect(body).toMatchObject({ handle: "reviewer", name: "Reviewer", description: "", instructions: "" });
    expect(body).not.toHaveProperty("color");
    expect(body.permissions.tool).toEqual({ mode: "allowlist", allow: ["read"] });
    expect(body.permissions.credential).toEqual({ mode: "allowlist", allow: ["npm-token", "pypi-token"] });
    expect(body.permissions.model).toEqual({ mode: "all" });
    expect(saved).toHaveBeenCalledWith(SAVED);
    expect(await screen.findByText("Đã tạo agent @reviewer")).toBeInTheDocument();
  });

  it("keeps a refusal of the hub in the dialog, with what it said", async () => {
    const user = userEvent.setup();
    api.answer = answer(() => json({ error: "conflict", message: "@reviewer is taken by another agent" }, 409));
    renderVi(<Harness onSaved={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Agent mới" });
    await user.type(within(dialog).getByLabelText("Handle"), "reviewer");
    await user.type(within(dialog).getByLabelText("Tên"), "Reviewer");
    await user.click(within(dialog).getByTestId("agent-save"));
    const error = await within(dialog).findByTestId("admin-dialog-error");
    expect(error).toHaveTextContent("Hub từ chối handle");
    expect(error).toHaveTextContent("@reviewer is taken by another agent");
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
  });

  it("edits the default agent's name and colour only, and saves nothing until something changed", async () => {
    const user = userEvent.setup();
    const current: Agent = { ...SAVED, handle: "demo", name: "demo", is_default: true, restricted: [], version: 4 };
    let patched: unknown = null;
    api.answer = (request) => {
      if (request.method === "PATCH") {
        return request.json().then((body) => {
          patched = body;
          return json({ ...current, name: "Demo team", version: 5 });
        });
      }
      return answer(() => json({}, 500))(request);
    };
    renderVi(<Harness agent={current} onSaved={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Sửa @demo" });
    expect(within(dialog).getByTestId("agent-default-note")).toBeInTheDocument();
    expect(within(dialog).queryByTestId("agent-permissions-form")).toBeNull();
    expect(within(dialog).getByTestId("agent-save")).toBeDisabled();
    await user.clear(within(dialog).getByLabelText("Tên"));
    await user.type(within(dialog).getByLabelText("Tên"), "Demo team");
    await user.click(within(dialog).getByRole("radio", { name: "Xanh lá" }));
    await user.click(within(dialog).getByTestId("agent-save"));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    expect(patched).toEqual({ name: "Demo team", color: "#16a34a", if_version: 4 });
  });
});
