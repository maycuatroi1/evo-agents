// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { hasOtherTurns, isSessionTurn, turnsInOrder, turnText } from "./chat-session";
import { ChatSessionHead } from "./chat-session-turns";
import type { Run } from "./queries";
import { RunsTable } from "./runs-table";

vi.mock("next/navigation", () => ({
  usePathname: () => "/p/demo/runs",
  useParams: () => ({ project: "demo" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const api = vi.hoisted(() => ({ seen: [] as URL[], turns: [] as unknown[] }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        api.seen.push(url);
        if (url.pathname === "/v1/projects/demo/runs") {
          return Response.json({ runs: api.turns, total: api.turns.length, counts: {}, limit: 200, offset: 0 });
        }
        return Response.json({ error: "not_found", message: url.pathname }, { status: 404 });
      },
    }),
}));

const SESSION = "@reviewer in Release talk: please fix the flaky wait test";

function turn(id: number, n: number, words: string, state: Run["state"] = "done"): Run {
  return {
    id,
    project: "demo",
    kind: "chat",
    profile: "reviewer",
    plan_id: "",
    step_key: null,
    title: `@reviewer in Release talk: ${words}`,
    state,
    worker_id: 3,
    worker: "studio",
    pinned_worker_id: 3,
    dispatched_by: "octo",
    runtime: "claude-code",
    attempt: 1,
    max_attempts: 3,
    repos: [],
    branch: "chat/40",
    error: null,
    queued_at: "2026-10-19T03:00:00Z",
    started_at: "2026-10-19T03:00:05Z",
    finished_at: null,
    session_run_id: 40,
    session_turn: n,
    session_turns: 3,
    session_title: SESSION,
  } as unknown as Run;
}

const FIRST = turn(40, 1, "please fix the flaky wait test");
const SECOND = turn(43, 2, "one more thing: the retries");
const THIRD = turn(47, 3, "and the docs?", "running");

function show(ui: ReactElement) {
  return renderVi(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  api.seen = [];
  api.turns = [THIRD, SECOND, FIRST]; // newest first, as the API lists them
});

describe("chat session turns", () => {
  it("say a turn's words without the session's start, and order the turns as the conversation went", () => {
    expect(turnText(SECOND.title, SESSION, "reviewer")).toBe("one more thing: the retries");
    expect(turnText(FIRST.title, SESSION, "reviewer")).toBe("please fix the flaky wait test");
    expect(turnText("@other in Release talk: hi", SESSION, "reviewer")).toBe("@other in Release talk: hi");
    expect(turnText("@reviewer in Release talk: ", SESSION, "reviewer")).toBe("@reviewer in Release talk: ");
    expect(turnText(SECOND.title, null, "reviewer")).toBe(SECOND.title);
    expect(turnText(null, SESSION, "reviewer")).toBeNull();
    expect(turnsInOrder([THIRD, FIRST, SECOND]).map((run) => run.id)).toEqual([40, 43, 47]);
    expect([isSessionTurn(THIRD), hasOtherTurns(THIRD), hasOtherTurns({ ...FIRST, session_turns: 1 })]).toEqual([true, true, false]);
    expect(isSessionTurn({ ...FIRST, kind: "step", session_run_id: null } as Run)).toBe(false);
  });

  it("list a session once in a grouped table, as its latest turn, and open into its turns", async () => {
    show(<RunsTable runs={[THIRD]} caption="Runs" viewer={null} grouped />);
    const table = screen.getByTestId("runs-table");
    const title = within(table).getByRole("link", { name: SESSION });
    expect(title).toHaveAttribute("href", "/p/demo/runs/47");
    expect(within(table).getByText("Phiên của octo với @reviewer trong group chat")).toBeInTheDocument();
    expect(within(table).getByTestId("run-state")).toHaveAttribute("data-status", "running");
    const toggle = within(table).getByRole("button", { name: "3 lượt" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).not.toHaveAttribute("aria-controls");

    await userEvent.click(toggle);
    expect(toggle).toHaveFocus(); // the same button, still where the keyboard left it
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const turns = await screen.findByRole("list", { name: `Các lượt của phiên ${SESSION}` });
    expect(toggle).toHaveAttribute("aria-controls", turns.parentElement?.id);
    const rows = within(turns).getAllByTestId("session-turn");
    expect(rows.map((row) => row.getAttribute("data-turn-run-id"))).toEqual(["40", "43", "47"]);
    expect(rows[1]).toHaveTextContent("Lượt 2");
    expect(within(rows[1]).getByRole("link", { name: "one more thing: the retries" })).toHaveAttribute("href", "/p/demo/runs/43");
    const asked = api.seen.find((url) => url.searchParams.get("session_run_id") === "40");
    expect(asked?.searchParams.get("limit")).toBe("200");

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("session-turns")).toBeNull();
  });

  it("keep each run of an ungrouped table as it is", () => {
    show(<RunsTable runs={[SECOND]} caption="Runs" viewer={null} />);
    const table = screen.getByTestId("runs-table");
    expect(within(table).getByRole("link", { name: SECOND.title ?? "" })).toHaveAttribute("href", "/p/demo/runs/43");
    expect(within(table).queryByTestId("runs-session-toggle")).toBeNull();
  });

  it("head a turn's page with the session's title, which turn it is, and a link to each turn", async () => {
    show(<ChatSessionHead run={SECOND} />);
    const head = screen.getByTestId("run-session");
    expect(within(head).getByTestId("run-session-title")).toHaveTextContent(SESSION);
    expect(within(head).getByTestId("run-turn")).toHaveTextContent("Lượt 2 trong 3");
    expect(within(head).getByTestId("run-turn-text")).toHaveTextContent("one more thing: the retries");
    const nav = within(head).getByRole("navigation", { name: "Các lượt của phiên này" });
    const links = await within(nav).findAllByTestId("run-turn-link");
    expect(links.map((link) => [link.textContent, link.getAttribute("href"), link.getAttribute("aria-current")])).toEqual([
      ["1", "/p/demo/runs/40", null],
      ["2", "/p/demo/runs/43", "page"],
      ["3", "/p/demo/runs/47", null],
    ]);
    expect(links[2]).toHaveAccessibleName("Lượt 3: and the docs?");
  });

  it("head the page of a session's only turn with its title alone", () => {
    show(<ChatSessionHead run={{ ...FIRST, session_turns: 1 }} />);
    const head = screen.getByTestId("run-session");
    expect(within(head).getByTestId("run-session-title")).toHaveTextContent(SESSION);
    expect(within(head).queryByRole("navigation")).toBeNull();
    expect(api.seen).toEqual([]);
  });
});
