// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import type { Run } from "./queries";
import { RerunDialog } from "./rerun-dialog";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as Request[],
  push: vi.fn(),
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

vi.mock("next/navigation", () => ({
  usePathname: () => "/p/demo/runs/40",
  useParams: () => ({ project: "demo" }),
  useRouter: () => ({ push: api.push, replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-9" } });

const account = (name: string, projects: string[], extra: Record<string, unknown> = {}) => ({
  name,
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  auth: "token",
  projects,
  workers: [],
  weight: 100,
  priority: 100,
  paused_at: null,
  state: "ready",
  cooldown_until: null,
  cooldown_reason: null,
  ...extra,
});

const ACCOUNTS = [
  account("claude-b", ["demo"]),
  account("claude-a", ["demo"], { paused_at: "2026-10-05T07:00:00Z", state: "paused" }),
  account("claude-c", ["demo"], { workers: ["lab-ws-01"] }),
  account("elsewhere", ["other"]),
];

const RUN = { id: 40, project: "demo", plan_id: "rollout", step_key: "2", state: "failed" } as Run;

function answer(rerun: (request: Request) => Response) {
  return (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/v1/accounts") return json(ACCOUNTS);
    if (url.pathname === "/v1/projects/demo/runs/40/rerun" && request.method === "POST") return rerun(request);
    return json({ error: "not_found", message: url.pathname }, 404);
  };
}

function Harness() {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <RerunDialog run={RUN} open={open} onOpenChange={setOpen} errorView={(error) => <p role="alert">{`${error.text} ${error.detail ?? ""}`}</p>} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

const rerunPost = () => api.seen.find((request) => request.method === "POST" && request.url.endsWith("/rerun"));

beforeEach(() => {
  api.answer = null;
  api.seen = [];
  api.push.mockReset();
});

describe("RerunDialog", () => {
  it("keeps the run's account by default and sends no body", async () => {
    const user = userEvent.setup();
    api.answer = answer(() => json({ ...RUN, id: 41, state: "queued", parent_run_id: 40 }, 201));
    renderVi(<Harness />);
    const dialog = screen.getByRole("dialog");
    const select = within(dialog).getByTestId("dispatch-account-select") as HTMLSelectElement;
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(5));
    const options = within(select).getAllByRole("option") as HTMLOptionElement[];
    // keep, the rules, then the accounts bound to the project by name; a paused one cannot be picked
    expect(options.map((option) => option.value)).toEqual(["=keep", "", "claude-a", "claude-b", "claude-c"]);
    expect(options[2]).toBeDisabled();
    expect(options[4].textContent).toContain("lab-ws-01");
    expect(select.value).toBe("=keep");

    await user.click(within(dialog).getByTestId("rerun-confirm"));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    expect(rerunPost()?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await rerunPost()?.clone().text()).toBe("");
    expect(api.push).toHaveBeenCalledWith("/p/demo/runs/41");
  });

  it("sends the account picked, or asks the rules", async () => {
    const user = userEvent.setup();
    api.answer = answer(() => json({ ...RUN, id: 42, state: "queued" }, 201));
    renderVi(<Harness />);
    const select = (await screen.findByTestId("dispatch-account-select")) as HTMLSelectElement;
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(5));
    await user.selectOptions(select, "claude-b");
    await user.click(screen.getByTestId("rerun-confirm"));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    expect(await rerunPost()?.clone().json()).toEqual({ account: "claude-b", account_by_rule: false });
  });

  it("stays open with the hub's reason when the account cannot go", async () => {
    const user = userEvent.setup();
    const message = "run 40 took its account for CLAUDE_CODE_OAUTH_TOKEN by hand, and it cannot go with a rerun";
    api.answer = answer(() => json({ error: "unprocessable", message, request_id: "rid-9" }, 422));
    renderVi(<Harness />);
    await user.click(screen.getByTestId("rerun-confirm"));
    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
    expect(api.push).not.toHaveBeenCalled();
  });
});
