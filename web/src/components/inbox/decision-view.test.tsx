// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast as sonner } from "sonner";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { DecisionCard } from "./decision-view";
import { InboxBell } from "./inbox-bell";
import type { Decision } from "./queries";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { url: string; method: string; headers: Headers; body: string }[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push({ url: request.url, method: request.method, headers: request.headers, body: await request.clone().text() });
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/p/demo/runs/12" }));

const DECISION: Decision = {
  id: 7,
  project: "demo",
  run_id: 12,
  run_state: "waiting",
  plan_id: "rollout",
  step_key: "3",
  category: "deploy",
  question: "Deploy the build to staging now?",
  context: "The checkpoint deploys **staging**.\n\n<script>alert(1)</script>",
  options: [
    { key: "deploy", label: "Deploy to staging now", description: "Takes ten minutes.", recommended: true, waits: false },
    { key: "wait", label: "Wait until tomorrow", description: null, recommended: false, waits: false },
  ],
  recommended: "deploy",
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
};

const ANSWERED: Decision = {
  ...DECISION,
  run_state: "running",
  state: "answered",
  answer_option: "wait",
  answer_text: "after the backup",
  answered_by: "octo",
  answer_run_id: 12,
  answered_at: "2026-10-05T07:05:00Z",
};

type Props = Partial<Parameters<typeof DecisionCard>[0]>;

function setup(decision: Decision = DECISION, login = "octo", props: Props = { parksAt: null }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  client.setQueryData(queryKeys.whoami, { login, admin: false, token: {}, grants: [{ project: "demo", role: "writer", max_level: "internal" }] });
  const view = renderVi(
    <QueryClientProvider client={client}>
      <DecisionCard decision={decision} where="inbox" {...props} />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), client, view };
}

/** The toast whose words hold `text`; toasts of earlier tests may still be closing. */
async function findToast(text: string) {
  await screen.findByText(text);
  const found = screen.getAllByTestId("toast").filter((toast) => toast.textContent?.includes(text));
  expect(found).toHaveLength(1);
  return found[0];
}

function posts() {
  return api.seen.filter((request) => request.method === "POST" && !request.url.endsWith("/csrf"));
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
  sonner.dismiss();
});

describe("DecisionCard", () => {
  it("shows why the agent picks its option, how to undo the act and the evidence, each with a link to its page", () => {
    setup({
      ...DECISION,
      recommended_reason: "Staging is idle and the build passed every check.",
      reversible: true,
      undo: "Redeploy the previous tag from the Releases page.",
      evidence: ["run:11", "P-3/E-2", "https://ci.example.test/builds/88", "session:3f2a9c1e-0000"],
    });
    const grounds = screen.getByRole("region", { name: "Lý do, cách hoàn tác và bằng chứng" });
    expect(within(grounds).getByTestId("decision-reason")).toHaveTextContent("Vì sao chọn “Deploy to staging now”: Staging is idle and the build passed every check.");
    expect(within(grounds).getByTestId("decision-undo")).toHaveTextContent("Hoàn tác được: Redeploy the previous tag from the Releases page.");
    expect(within(grounds).getByText("Bằng chứng (4)")).toBeInTheDocument();
    const links = within(grounds).getAllByTestId("decision-evidence-link");
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["Run #11", "/p/demo/runs/11"],
      ["P-3, bằng chứng E-2", "/p/demo/problems/P-3"],
      ["ci.example.test/builds/88(mở trong tab mới)", "https://ci.example.test/builds/88"],
    ]);
    expect(links[2]).toHaveAttribute("target", "_blank");
    expect(links[2]).toHaveAttribute("rel", "noopener noreferrer");
    const items = within(grounds).getAllByTestId("decision-evidence-item");
    expect(items[3]).toHaveTextContent("Phiên 3f2a9c1e");
    expect(within(items[3]).queryByRole("link")).toBeNull();
  });

  it("says an act cannot be undone, and shows nothing of it for a question that says none, or once answered", () => {
    const first = setup({ ...DECISION, reversible: false });
    expect(screen.getByTestId("decision-irreversible")).toHaveTextContent("Không hoàn tác được.");
    expect(screen.queryByTestId("decision-reason")).toBeNull();
    expect(screen.queryByTestId("decision-evidence")).toBeNull();
    first.view.unmount();

    const second = setup({ ...DECISION, reversible: true, undo: null, recommended_reason: "Cheapest.", recommended: null, evidence: [] });
    expect(screen.getByTestId("decision-undo")).toHaveTextContent("Hoàn tác được: agent chưa nói cách làm.");
    expect(screen.getByTestId("decision-reason")).toHaveTextContent("Vì sao agent chọn: Cheapest.");
    second.view.unmount();

    const third = setup(DECISION);
    expect(screen.queryByTestId("decision-grounds")).toBeNull();
    third.view.unmount();

    setup({ ...ANSWERED, recommended_reason: "Cheapest.", reversible: false, evidence: ["run:11"] });
    expect(screen.queryByTestId("decision-grounds")).toBeNull();
  });

  it("heads the card with Waiting for you and where the decision comes from, then the question and its context as safe Markdown", () => {
    setup();
    const head = screen.getByTestId("decision-head");
    expect(within(head).getByTestId("decision-state")).toHaveTextContent("Đang chờ bạn");
    expect(within(head).getByTestId("decision-category")).toHaveTextContent("Triển khai");
    expect(within(head).getByTestId("decision-run-link")).toHaveAttribute("href", "/p/demo/runs/12");
    expect(within(head).getByTestId("decision-run-link")).toHaveTextContent("Run #12");
    expect(within(head).getByTestId("decision-plan-link")).toHaveAttribute("href", "/p/demo/plans/rollout");
    expect(within(head).getByTestId("decision-step-link")).toHaveAttribute("href", "/p/demo/plans/rollout/steps/3");
    expect(within(head).getByTestId("decision-step-link")).toHaveTextContent("bước 3");
    expect(within(head).getByTestId("decision-timing")).toHaveTextContent(/^hỏi /);
    expect(screen.getByRole("heading", { level: 2, name: "Deploy the build to staging now?" })).toBeInTheDocument();
    const context = screen.getByTestId("decision-context-markdown");
    expect(context.querySelector("strong")).toHaveTextContent("staging");
    expect(context.querySelector("script")).toBeNull();
    expect(context).toHaveTextContent("<script>alert(1)</script>");
  });

  it("offers the options as radio cards with the agent's pick tagged and chosen, and the kit's footer", () => {
    setup();
    const deploy = screen.getByTestId("decision-choice-deploy");
    expect(within(deploy).getByTestId("decision-recommended")).toHaveTextContent("Agent đề xuất");
    expect(within(screen.getByTestId("decision-choice-wait")).queryByTestId("decision-recommended")).toBeNull();
    const picked = screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked);
    expect(picked).toHaveLength(1);
    expect(picked[0]).toHaveAccessibleName("Deploy to staging now Agent đề xuất");
    expect(picked[0]).toHaveAccessibleDescription("Takes ten minutes.");
    expect(screen.getByRole("group", { name: "Câu trả lời của bạn" })).toBeInTheDocument();
    expect(screen.getByTestId("decision-form-hint")).toHaveTextContent("Agent làm tiếp ngay khi bạn trả lời.");
    expect(screen.getByTestId("decision-send")).toHaveAccessibleName("Gửi câu trả lời");
    expect(screen.getByTestId("decision-send")).toHaveAttribute("aria-keyshortcuts", "Meta+Enter Control+Enter");
    expect(screen.getByTestId("decision-send-kbd")).toHaveTextContent(/↵/);
    // The run waits on no terminal: no Take over.
    expect(screen.queryByTestId("decision-takeover")).toBeNull();
  });

  it("sends the agent's pick with one click", async () => {
    api.answer = () => Response.json({ ...ANSWERED, answer_option: "deploy", answer_text: null });
    const { user } = setup();
    await user.click(screen.getByTestId("decision-send"));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0].body)).toEqual({ option: "deploy" });
  });

  it("refuses an empty answer without asking the hub, once the choice is cleared", async () => {
    const { user } = setup();
    await user.click(screen.getByTestId("decision-clear-option"));
    expect(screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked)).toHaveLength(0);
    expect(screen.getByLabelText("Câu trả lời của bạn", { selector: "textarea" })).toHaveAttribute("placeholder", "Viết câu trả lời cho agent");
    await user.click(screen.getByTestId("decision-send"));
    expect(screen.getByTestId("decision-problem")).toHaveTextContent("Hãy chọn một phương án hoặc viết câu trả lời trước.");
    expect(screen.getAllByRole("radio")[0]).toHaveFocus();
    expect(api.seen).toHaveLength(0);
  });

  it("sends the option and the words with the session's CSRF header, and says so in a toast", async () => {
    api.answer = () => Response.json(ANSWERED);
    const { user } = setup();
    await user.click(screen.getByRole("radio", { name: /Wait until tomorrow/ }));
    await user.type(screen.getByTestId("decision-text"), "  after the backup ");
    await user.click(screen.getByTestId("decision-send"));
    const toast = await findToast("Đã gửi câu trả lời tới run #12");
    expect(toast).toHaveAttribute("data-tone", "success");
    // The toast links back to the run, unless the run's page is the one shown, as here.
    expect(within(toast).queryByRole("link")).toBeNull();
    const [post] = posts();
    expect(post.url).toBe("http://hub.test/v1/projects/demo/decisions/7/answer");
    expect(post.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(JSON.parse(post.body)).toEqual({ option: "wait", text: "after the backup" });
  });

  it("sends with Ctrl and Enter from the note", async () => {
    api.answer = () => Response.json(ANSWERED);
    const { user } = setup();
    await user.type(screen.getByTestId("decision-text"), "ship it");
    await user.keyboard("{Control>}{Enter}{/Control}");
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0].body)).toEqual({ option: "deploy", text: "ship it" });
  });

  it("says a decision answered meanwhile cannot take another answer, then reads it again", async () => {
    api.answer = (request) =>
      request.method === "POST"
        ? Response.json({ error: "conflict", message: "decision 7 is answered, not open: it takes no answer any more", request_id: "rid-4" }, { status: 409 })
        : Response.json(ANSWERED);
    const onConflict = vi.fn();
    const { user } = setup(DECISION, "octo", { parksAt: null, onConflict });
    await user.click(screen.getByTestId("decision-send"));
    const alert = await findToast("Không gửi được câu trả lời");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("Quyết định đã được trả lời hoặc đã đóng trong lúc đó");
    expect(alert).toHaveTextContent("Hub báo: decision 7 is answered, not open");
    expect(alert).toHaveTextContent("rid-4");
    await waitFor(() => expect(onConflict).toHaveBeenCalledWith(ANSWERED));
    expect(api.seen.some((request) => request.method === "GET" && request.url === "http://hub.test/v1/projects/demo/decisions/7")).toBe(true);
  });

  it("folds a long context after four lines and unfolds it", async () => {
    const height = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(400);
    const client = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(84);
    try {
      const { user } = setup();
      const toggle = await screen.findByTestId("decision-context-toggle");
      expect(screen.getByTestId("decision-context")).toHaveAttribute("data-folded", "true");
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      expect(toggle).toHaveTextContent("Xem thêm");
      await user.click(toggle);
      expect(toggle).toHaveAttribute("aria-expanded", "true");
      expect(toggle).toHaveTextContent("Thu gọn");
      expect(screen.getByTestId("decision-context")).toHaveAttribute("data-folded", "false");
    } finally {
      height.mockRestore();
      client.mockRestore();
    }
  });

  it("says when a waiting run parks, and since when a parked one has", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-10-05T08:00:00Z"));
    try {
      setup(DECISION, "octo", { parksAt: "2026-10-06T07:00:30Z" });
      expect(screen.getByTestId("decision-parks")).toHaveTextContent("tạm gác sau 23 giờ 0 phút");
    } finally {
      vi.useRealTimers();
    }
  });

  it("says a parked run resumes once answered", () => {
    setup({ ...DECISION, run_state: "parked" }, "octo", { parksAt: "2026-10-06T07:00:00Z" });
    expect(screen.getByTestId("decision-parks")).toHaveTextContent(/^tạm gác /);
    expect(screen.getByTestId("decision-form-hint")).toHaveTextContent("Run chạy tiếp trên cùng worker ngay khi bạn trả lời.");
  });

  it("asks the hub's overview when the run parks, when the caller does not know", async () => {
    api.answer = (request) =>
      request.url.endsWith("/v1/me/overview")
        ? Response.json({ open_decisions: [{ id: 7, project: "demo", parks_at: "2099-01-01T00:00:00Z" }, { id: 8, project: "demo", parks_at: null }] })
        : new Response(null, { status: 404 });
    setup(DECISION, "octo", {});
    expect(await screen.findByTestId("decision-parks")).toHaveTextContent(/^tạm gác sau /);
  });

  it("offers Take over while the owner may open the run's terminal", async () => {
    api.answer = (request) => {
      if (request.url.endsWith("/v1/projects/demo/runs/12")) return Response.json({ id: 12, project: "demo", state: "running", dispatched_by: "octo", worker_id: 5 });
      if (request.url.endsWith("/v1/workers/5")) return Response.json({ id: 5, owner: "octo", allow_web_terminal: true, name: "box", status: "online" });
      return new Response(null, { status: 404 });
    };
    setup({ ...DECISION, run_state: "running" });
    const takeOver = await screen.findByTestId("decision-takeover");
    expect(takeOver).toHaveAttribute("href", "/p/demo/runs/12?view=terminal");
    expect(takeOver).toHaveAccessibleName("Tiếp quản run #12 trong terminal");
  });

  it("takes over on the run's own page through its Terminal tab", async () => {
    api.answer = (request) => {
      if (request.url.endsWith("/v1/projects/demo/runs/12")) return Response.json({ id: 12, project: "demo", state: "running", dispatched_by: "octo", worker_id: 5 });
      if (request.url.endsWith("/v1/workers/5")) return Response.json({ id: 5, owner: "octo", allow_web_terminal: true, name: "box", status: "online" });
      return new Response(null, { status: 404 });
    };
    const onTakeOver = vi.fn();
    setup({ ...DECISION, run_state: "running" }, "octo", { parksAt: null, onTakeOver });
    fireEvent.click(await screen.findByTestId("decision-takeover"));
    expect(onTakeOver).toHaveBeenCalledOnce();
  });

  it("does not offer Take over on a worker that keeps its terminal closed", async () => {
    api.answer = (request) => {
      if (request.url.endsWith("/v1/projects/demo/runs/12")) return Response.json({ id: 12, project: "demo", state: "running", dispatched_by: "octo", worker_id: 5 });
      if (request.url.endsWith("/v1/workers/5")) return Response.json({ id: 5, owner: "octo", allow_web_terminal: false, name: "box", status: "online" });
      return new Response(null, { status: 404 });
    };
    setup({ ...DECISION, run_state: "running" });
    await waitFor(() => expect(api.seen.some((request) => request.url.endsWith("/v1/workers/5"))).toBe(true));
    expect(screen.queryByTestId("decision-takeover")).toBeNull();
  });

  it("shows an answered decision as one block: who answered and when, the option and the words, delivery and the resumed run", () => {
    setup({
      ...DECISION,
      run_state: "done",
      state: "answered",
      answer_option: "deploy",
      answer_text: "Go ahead.",
      answered_by: "octo",
      answer_run_id: 15,
      answered_at: "2026-10-05T07:05:00Z",
      delivered_at: null,
    });
    expect(screen.queryByTestId("decision-form")).toBeNull();
    expect(screen.queryByTestId("decision-options")).toBeNull();
    expect(screen.queryByTestId("decision-context")).toBeNull();
    const head = screen.getByTestId("decision-head");
    expect(within(head).getByTestId("decision-state")).toHaveTextContent("Đã trả lời");
    expect(within(head).getByTestId("decision-answered-by")).toHaveTextContent(/^octo đã trả lời/);
    const answer = screen.getByTestId("decision-answer");
    expect(within(answer).getByTestId("decision-answer-option")).toHaveTextContent("Deploy to staging now");
    expect(within(answer).getByTestId("decision-answer-option")).toHaveAttribute("data-key", "deploy");
    expect(within(answer).getByTestId("decision-answer-text")).toHaveTextContent("Go ahead.");
    expect(within(answer).getByTestId("decision-delivery")).toHaveAttribute("data-delivered", "false");
    expect(within(answer).getByTestId("decision-resumed").querySelector("a")).toHaveAttribute("href", "/p/demo/runs/15");
  });

  it("keeps a decision open on an option that waits: tags it, refuses words with it, and says so in a toast", async () => {
    const later = { key: "later", label: "Not yet", description: null, recommended: false, waits: true };
    const decision = { ...DECISION, options: [...DECISION.options, later] };
    api.answer = () => Response.json({ ...decision, held_at: "2026-10-05T07:04:00Z" });
    const { user } = setup(decision);
    const card = screen.getByTestId("decision-choice-later");
    expect(within(card).getByTestId("decision-option-waits")).toHaveTextContent("Giữ mở");
    expect(within(screen.getByTestId("decision-choice-deploy")).queryByTestId("decision-option-waits")).toBeNull();
    await user.click(screen.getByRole("radio", { name: /Not yet/ }));
    await user.type(screen.getByTestId("decision-text"), "ask me after the 30th");
    await user.click(screen.getByTestId("decision-send"));
    expect(screen.getByTestId("decision-problem")).toHaveTextContent("Phương án này giữ decision mở");
    expect(posts()).toHaveLength(0);
    await user.clear(screen.getByTestId("decision-text"));
    await user.click(screen.getByTestId("decision-send"));
    expect(await findToast("Vẫn để mở")).toHaveTextContent("Run #12 vẫn chờ câu trả lời của bạn");
    expect(JSON.parse(posts()[0].body)).toEqual({ option: "later" });
  });

  it("says when the owner said not yet on a decision still open", () => {
    setup({ ...DECISION, held_at: "2026-10-05T07:04:00Z" });
    expect(screen.getByTestId("decision-held")).toHaveTextContent("bạn đã chọn chưa trả lời");
  });

  it("names the hub as what woke a wait, whose time or event came", () => {
    setup({
      ...DECISION,
      category: "wait",
      run_state: "running",
      state: "answered",
      answer_text: "Decision #7 (wait) woke: its time came (2026-10-30T09:00:00Z).",
      answered_by: null,
      answer_run_id: 12,
      answered_at: "2026-10-30T09:00:30Z",
      wake_at: "2026-10-30T09:00:00Z",
    });
    const head = screen.getByTestId("decision-head");
    expect(within(head).getByTestId("decision-category")).toHaveTextContent("Chờ");
    expect(within(head).getByTestId("decision-answered-by")).toHaveTextContent(/^hub tự đánh thức/);
  });

  it("shows another member the options and why they cannot answer, but no form", () => {
    setup(DECISION, "mona");
    expect(screen.queryByTestId("decision-form")).toBeNull();
    expect(screen.getByTestId("decision-state")).toHaveTextContent("Đang chờ octo");
    expect(screen.getByTestId("decision-locked")).toHaveTextContent("Chỉ octo, người được hỏi quyết định này của run #12, mới trả lời được.");
    const options = screen.getAllByTestId("decision-option");
    expect(options.map((option) => option.getAttribute("data-key"))).toEqual(["deploy", "wait"]);
    expect(within(options[0]).getByTestId("decision-recommended")).toHaveTextContent("Agent đề xuất");
  });

  it("names only the step on the run's own page, the question an h3", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    client.setQueryData(queryKeys.whoami, { login: "octo", admin: false, token: {}, grants: [] });
    renderVi(
      <QueryClientProvider client={client}>
        <DecisionCard decision={DECISION} where="run" parksAt={null} />
      </QueryClientProvider>,
    );
    expect(screen.getByRole("heading", { level: 3, name: "Deploy the build to staging now?" })).toBeInTheDocument();
    expect(screen.queryByTestId("decision-run-link")).toBeNull();
    expect(screen.queryByTestId("decision-plan-link")).toBeNull();
    expect(screen.getByTestId("decision-step-link")).toHaveTextContent("bước 3");
  });

  it("names an author run's decision, which has no plan, by its run and the run's title, and links to no plan", async () => {
    api.answer = (request) =>
      new URL(request.url).pathname === "/v1/projects/demo/runs/12"
        ? Response.json({ id: 12, project: "demo", kind: "author", title: "Plans from the web", state: "waiting" })
        : Response.json({ error: "not_found", message: "no" }, { status: 404 });
    setup({ ...DECISION, plan_id: null, step_key: null, category: "scope" });
    const head = screen.getByTestId("decision-head");
    expect(within(head).getByTestId("decision-run-link")).toHaveTextContent("Author run #12");
    expect(within(head).getByTestId("decision-run-link")).toHaveAttribute("href", "/p/demo/runs/12");
    await waitFor(() => expect(within(head).getByTestId("decision-where")).toHaveTextContent("demo / Plans from the web"));
    expect(within(head).queryByTestId("decision-plan-link")).toBeNull();
    expect(within(head).queryByTestId("decision-step-link")).toBeNull();
    expect(screen.getByTestId("decision").querySelector('a[href*="/plans/"]')).toBeNull();
  });
});

describe("InboxBell", () => {
  afterEach(() => vi.useRealTimers());

  it("links to the Inbox and says the unread number and the decisions waiting in its name", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    api.answer = () => Response.json({ unread: 120, open_decisions: 2 });
    renderVi(
      <QueryClientProvider client={client}>
        <InboxBell />
      </QueryClientProvider>,
    );
    const bell = screen.getByTestId("inbox-bell");
    expect(bell).toHaveAttribute("href", "/inbox");
    await waitFor(() => expect(bell).toHaveAttribute("data-unread", "120"));
    expect(bell).toHaveAccessibleName("Inbox: 120 thông báo chưa đọc, 2 quyết định chờ bạn trả lời");
    expect(screen.getByTestId("inbox-bell-count")).toHaveTextContent("99+");
  });
});
