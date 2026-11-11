// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast as sonner } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import type { WaitingOn, WorkflowRunWait } from "@/lib/waits";
import { renderVi } from "@/test/render";

import { DecisionCard } from "./decision-view";
import type { Decision } from "./queries";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { url: string; method: string; body: string }[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push({
          url: request.url,
          method: request.method,
          body: await request.clone().text(),
        });
        if (request.url.endsWith("/v1/auth/web/csrf"))
          return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/p/demo/runs/12" }));

const WORKFLOW: WorkflowRunWait = {
  repo: "acme/shop",
  run_id: 991,
  attempt: 1,
  workflow: "deploy",
  title: "Deploy 5da2153",
  branch: "main",
  sha: "5da2153aa11bb22",
  event: "push",
  status: "in_progress",
  conclusion: null,
  url: "https://github.com/acme/shop/actions/runs/991",
  created_at: "2026-10-05T07:00:00Z",
  started_at: "2026-10-05T07:00:00Z",
  updated_at: "2026-10-05T07:04:00Z",
  read_at: "2026-10-05T07:04:12Z",
  typical_seconds: 452,
  jobs_total: 3,
  jobs: [
    {
      id: 1,
      name: "build",
      status: "completed",
      conclusion: "success",
      started_at: "2026-10-05T07:00:00Z",
      completed_at: "2026-10-05T07:02:00Z",
      url: "https://github.com/acme/shop/actions/runs/991/job/1",
      steps_total: 2,
      steps: [
        {
          number: 1,
          name: "Checkout",
          status: "completed",
          conclusion: "success",
          started_at: null,
          completed_at: null,
          url: "https://github.com/acme/shop/actions/runs/991/job/1#step:1:1",
        },
        {
          number: 2,
          name: "Build",
          status: "completed",
          conclusion: "success",
          started_at: null,
          completed_at: null,
          url: "https://github.com/acme/shop/actions/runs/991/job/1#step:2:1",
        },
      ],
    },
    {
      id: 2,
      name: "deploy",
      status: "in_progress",
      conclusion: null,
      started_at: "2026-10-05T07:02:00Z",
      completed_at: null,
      url: "https://github.com/acme/shop/actions/runs/991/job/2",
      steps_total: 3,
      steps: [
        {
          number: 1,
          name: "Push image",
          status: "completed",
          conclusion: "success",
          started_at: null,
          completed_at: null,
          url: "https://github.com/acme/shop/actions/runs/991/job/2#step:1:1",
        },
        {
          number: 2,
          name: "Roll out",
          status: "in_progress",
          conclusion: null,
          started_at: null,
          completed_at: null,
          url: "https://github.com/acme/shop/actions/runs/991/job/2#step:2:1",
        },
        {
          number: 3,
          name: "Health",
          status: "queued",
          conclusion: null,
          started_at: null,
          completed_at: null,
          url: "https://github.com/acme/shop/actions/runs/991/job/2#step:3:1",
        },
      ],
    },
    {
      id: 3,
      name: "notify",
      status: "queued",
      conclusion: null,
      started_at: null,
      completed_at: null,
      url: "https://github.com/acme/shop/actions/runs/991/job/3",
      steps_total: 0,
      steps: [],
    },
  ],
};

const WAIT: Decision = {
  id: 7,
  project: "demo",
  run_id: 12,
  run_state: "parked",
  plan_id: "rollout",
  step_key: "6",
  category: "wait",
  question: "The deploy of 5da2153 has to finish before the smoke test.",
  context: "Waits for github.workflow_run run_id=991",
  options: [
    {
      key: "now",
      label: "Go on now",
      description: null,
      recommended: false,
      waits: false,
    },
    {
      key: "stop",
      label: "Stop waiting",
      description: null,
      recommended: false,
      waits: false,
    },
  ],
  recommended: null,
  state: "open",
  owner: "octo",
  asked_of: "octo",
  answer_option: null,
  answer_text: null,
  answered_by: null,
  answer_run_id: null,
  asked_at: "2026-10-05T07:00:00Z",
  answered_at: null,
  delivered_at: null,
  wake_at: "2026-10-05T13:20:00Z",
  wake_on: { kind: "github.workflow_run", contains: { run_id: 991 } },
  waiting_on: { kind: "workflow_run", workflow_run: WORKFLOW },
};

function setup(decision: Decision = WAIT, login = "octo") {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity },
      mutations: { retry: false },
    },
  });
  client.setQueryData(queryKeys.whoami, {
    login,
    admin: false,
    token: {},
    grants: [{ project: "demo", role: "writer", max_level: "internal" }],
  });
  renderVi(
    <QueryClientProvider client={client}>
      <DecisionCard
        decision={decision}
        where="run"
        parkedAt="2026-10-05T07:01:00Z"
        parksAt={null}
      />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup() };
}

function posts() {
  return api.seen.filter(
    (request) => request.method === "POST" && !request.url.endsWith("/csrf"),
  );
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
  sonner.dismiss();
});

describe("WaitCard", () => {
  it("waiting on GitHub Actions: shows the workflow, its steps done as a bar, and its jobs and steps linking to GitHub", () => {
    setup();
    const card = screen.getByTestId("decision");
    expect(card).toHaveAttribute("data-wait", "workflow_run");
    expect(screen.getByTestId("wait-headline")).toHaveTextContent(
      "Đang chờ GitHub Actions",
    );
    expect(screen.getByTestId("wait-reason")).toHaveTextContent(
      "The deploy of 5da2153 has to finish before the smoke test.",
    );
    const source = screen.getByTestId("wait-workflow");
    expect(within(source).getByTestId("wait-workflow-link")).toHaveAttribute(
      "href",
      WORKFLOW.url,
    );
    expect(source).toHaveTextContent("acme/shop");
    expect(source).toHaveTextContent("main");
    expect(source).toHaveTextContent("5da2153");
    expect(screen.getByTestId("wait-workflow-now")).toHaveTextContent(
      "Bước 2 trên 3 của deploy: Roll out",
    );
    // 3 of the 5 steps the hub read are over; the queued job has none yet.
    const bar = screen.getByTestId("wait-workflow-bar");
    expect(bar).toHaveAttribute("aria-valuenow", "3");
    expect(bar).toHaveAttribute("aria-valuemax", "5");
    // The running job starts open, the done and queued ones folded.
    const jobs = screen.getAllByTestId("wait-job");
    expect(jobs.map((job) => job.getAttribute("data-open"))).toEqual([
      "false",
      "true",
      "false",
    ]);
    expect(within(jobs[1]).getByTestId("wait-job-link")).toHaveAttribute(
      "href",
      "https://github.com/acme/shop/actions/runs/991/job/2",
    );
    const steps = within(jobs[1]).getAllByTestId("wait-step");
    expect(within(steps[1]).getByRole("link")).toHaveAttribute(
      "href",
      "https://github.com/acme/shop/actions/runs/991/job/2#step:2:1",
    );
    expect(steps.map((step) => step.getAttribute("data-state"))).toEqual([
      "done",
      "running",
      "queued",
    ]);
    // The timeline: asked, parked (the run's page knows when), the live source, the agent goes on.
    expect(screen.getByTestId("wait-node-parked")).toHaveAttribute(
      "data-state",
      "done",
    );
    expect(screen.getByTestId("wait-node-source")).toHaveAttribute(
      "data-state",
      "current",
    );
    expect(screen.getByTestId("wait-node-goes-on")).toHaveAttribute(
      "data-state",
      "pending",
    );
    expect(screen.getByTestId("wait-wakes-when")).toHaveTextContent(
      "workflow run hoàn tất",
    );
    expect(screen.getByTestId("wait-at-latest")).toHaveTextContent("UTC");
  });

  it("waiting on a failed workflow: the failed job starts open and says at which step", () => {
    const failed: WorkflowRunWait = {
      ...WORKFLOW,
      status: "completed",
      conclusion: "failure",
      jobs: [
        WORKFLOW.jobs[0],
        {
          ...WORKFLOW.jobs[1],
          status: "completed",
          conclusion: "failure",
          steps: [
            WORKFLOW.jobs[1].steps[0],
            {
              ...WORKFLOW.jobs[1].steps[1],
              status: "completed",
              conclusion: "failure",
            },
          ],
        },
      ],
    };
    setup({
      ...WAIT,
      waiting_on: { kind: "workflow_run", workflow_run: failed },
    });
    const jobs = screen.getAllByTestId("wait-job");
    expect(jobs.map((job) => job.getAttribute("data-open"))).toEqual([
      "false",
      "true",
    ]);
    expect(jobs[1]).toHaveTextContent("Thất bại ở bước 2, Roll out");
    expect(screen.getByTestId("wait-workflow")).toHaveAttribute(
      "data-state",
      "failed",
    );
  });

  it.each<[string, WaitingOn, string]>([
    [
      "plan step",
      {
        kind: "plan_step",
        plan_step: {
          plan: "northwind-launch",
          step: "8",
          wanted: "done",
          title: "Ship the page",
          status: "in_progress",
          run: { id: 4480, kind: "plan", state: "running" },
        },
      },
      "northwind-launch, bước 8",
    ],
    [
      "agent answer",
      {
        kind: "agent_answer",
        agent_answer: {
          handle: "ea-researcher",
          project: "demo",
          run: { id: 4481, kind: "author", state: "running" },
          question: "Which vendor is cheaper?",
          chat_id: 5,
          thread_id: 100,
          last_activity_at: null,
        },
      },
      "@ea-researcher",
    ],
    [
      "merge check",
      {
        kind: "merge_check",
        merge_check: {
          id: 3,
          repo: "acme/shop",
          number: 580,
          title: "Fix cart",
          url: "https://github.com/acme/shop/pull/580",
          head_sha: "5da2153aa",
          state: "judging",
          reason: null,
          judges: [
            { round: "first", state: "passed", run_id: 1, model: null },
            { round: "first", state: "judging", run_id: 2, model: null },
            { round: "second", state: "pending", run_id: null, model: null },
          ],
        },
      },
      "PR #580 tại 5da2153",
    ],
    [
      "time",
      { kind: "time", time: { wake_at: "2026-10-06T02:00:00Z" } },
      "02:00 UTC",
    ],
    [
      "event",
      {
        kind: "event",
        event: {
          kind: "canvas.claim",
          name: null,
          contains: [{ key: "claim", value: "price" }],
        },
      },
      "canvas.claim",
    ],
  ])(
    "waiting on a %s: a source card in words, no raw JSON as its main content",
    (_, on, words) => {
      setup({ ...WAIT, waiting_on: on });
      const source = screen.getByTestId("wait-source");
      expect(source).toHaveAttribute("data-kind", on.kind);
      expect(source).toHaveTextContent(words);
      expect(source.textContent).not.toMatch(/[{}"]/);
      // The JSON is folded away under its own summary, closed.
      const raw = screen.getByTestId("wait-raw");
      expect(raw).not.toHaveAttribute("open");
      expect(within(raw).getByText("Lượt chờ như hub lưu")).toBeInTheDocument();
    },
  );

  it("waiting on something: says nobody needs to answer, with no Waiting for you and nothing amber", () => {
    setup();
    const card = screen.getByTestId("decision");
    expect(card).not.toHaveTextContent("Đang chờ bạn");
    expect(screen.queryByTestId("decision-state")).toBeNull();
    expect(card.innerHTML).not.toMatch(/attention/);
    expect(screen.getByTestId("wait-pill")).toHaveAttribute(
      "data-tone",
      "neutral",
    );
    expect(screen.getByTestId("wait-pill")).toHaveTextContent(
      "Đang chờ deploy 3/5",
    );
    expect(screen.getByTestId("wait-lead")).toHaveTextContent(
      "Không ai cần trả lời",
    );
  });

  it("waiting on something: the owner gets two secondary buttons and no answer field until one is chosen", () => {
    setup();
    expect(screen.queryByTestId("decision-form")).toBeNull();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getByTestId("wait-go-on")).toHaveTextContent("Đi tiếp ngay");
    expect(screen.getByTestId("wait-stop")).toHaveTextContent("Thôi chờ");
  });

  it("waiting on something: Go on now asks to confirm, then sends option now", async () => {
    api.answer = () =>
      Response.json({
        ...WAIT,
        state: "answered",
        answer_option: "now",
        answered_by: "octo",
        answered_at: "2026-10-05T07:05:00Z",
      });
    const { user } = setup();
    await user.click(screen.getByTestId("wait-go-on"));
    expect(posts()).toHaveLength(0);
    const confirm = screen.getByTestId("wait-confirm");
    expect(confirm).toHaveTextContent("Đánh thức agent ngay?");
    expect(
      within(confirm).getByLabelText("Ghi chú cho agent (không bắt buộc)"),
    ).toHaveValue("");
    await user.click(within(confirm).getByTestId("wait-send"));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].url).toBe(
      "http://hub.test/v1/projects/demo/decisions/7/answer",
    );
    expect(JSON.parse(posts()[0].body)).toEqual({ option: "now" });
  });

  it("waiting on something: Stop waiting sends option stop with the note, and Cancel sends nothing", async () => {
    api.answer = () =>
      Response.json({
        ...WAIT,
        state: "answered",
        answer_option: "stop",
        answer_text: "ship without it",
        answered_by: "octo",
        answered_at: "2026-10-05T07:05:00Z",
      });
    const { user } = setup();
    await user.click(screen.getByTestId("wait-stop"));
    await user.click(screen.getByTestId("wait-cancel"));
    expect(screen.queryByTestId("wait-confirm")).toBeNull();
    await user.click(screen.getByTestId("wait-stop"));
    await user.type(screen.getByTestId("wait-note"), "ship without it");
    await user.click(screen.getByTestId("wait-send"));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0].body)).toEqual({
      option: "stop",
      text: "ship without it",
    });
  });

  it("waiting on something: another member sees the panel with no buttons, and who may wake it", () => {
    setup(WAIT, "mona");
    expect(screen.queryByTestId("wait-actions")).toBeNull();
    expect(screen.getByTestId("wait-nobody")).toHaveTextContent(
      "Chỉ octo có thể đánh thức sớm hơn",
    );
  });

  it("waiting on nothing more: once the hub woke it, the panel says so and offers nothing", () => {
    setup({
      ...WAIT,
      run_state: "running",
      state: "answered",
      answered_by: null,
      answer_option: "now",
      answered_at: "2026-10-05T07:08:00Z",
    });
    expect(screen.getByTestId("wait-headline")).toHaveTextContent(
      "Điều run chờ đã đến",
    );
    expect(screen.getByTestId("wait-woke")).toHaveTextContent("bởi hub");
    expect(screen.getByTestId("wait-node-goes-on")).toHaveAttribute(
      "data-state",
      "done",
    );
    expect(screen.queryByTestId("wait-actions")).toBeNull();
  });
});
