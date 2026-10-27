// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { toast as sonner } from "sonner";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { credentialOption, footWords, MAX_SECRET_BYTES, sendWords, valueProblem } from "./credential-model";
import { AnswerFields, DecisionCard, useAnswerForm } from "./decision-view";
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
        api.seen.push({ url: request.url, method: request.method, body: await request.clone().text() });
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/inbox" }));

const VARIABLE = "NORTHWIND_DEPLOY_KEY";
const SECRET = "nw-test-value-0001";

const OPTIONS: Decision["options"] = [
  { key: "both", label: `Paste the value of ${VARIABLE} and let the run use my hub account`, description: "Both.", recommended: false, waits: false },
  { key: "value", label: `Paste the value of ${VARIABLE}`, description: "The hub keeps it as your secret.", recommended: false, waits: false },
  { key: "as_me", label: "Let the run use my hub account", description: "The run calls the hub as you.", recommended: false, waits: false },
  { key: "refuse", label: "Refuse", description: "The run gets nothing.", recommended: false, waits: false },
];

const CREDENTIAL: Decision = {
  id: 91,
  project: "northwind",
  run_id: 320,
  run_state: "waiting",
  plan_id: "squad-chat",
  step_key: "2",
  category: "credential",
  question: "The deploy needs the Dokploy API key. May I have it?",
  context: null,
  options: OPTIONS,
  recommended: null,
  state: "open",
  owner: "octo",
  asked_of: "octo",
  answer_option: null,
  answer_text: null,
  answered_by: null,
  answer_run_id: null,
  asked_at: "2026-10-27T07:00:00Z",
  answered_at: null,
  delivered_at: null,
  credential_request: { env_var: VARIABLE, secret_name: "northwind-deploy-key", hub_account: true },
};

const ANSWERED: Decision = {
  ...CREDENTIAL,
  run_state: "running",
  state: "answered",
  answer_option: "value",
  answer_text: "The hub kept the value of NORTHWIND_DEPLOY_KEY as their secret northwind-deploy-key of kind env.",
  answered_by: "octo",
  answer_run_id: 320,
  answered_at: "2026-10-27T07:05:00Z",
};

function client(login: string) {
  const made = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  made.setQueryData(queryKeys.whoami, { login, admin: false, token: {}, grants: [{ project: "northwind", role: "writer", max_level: "internal" }] });
  return made;
}

function setup(decision: Decision = CREDENTIAL, login = "octo") {
  const queries = client(login);
  const view = renderVi(
    <QueryClientProvider client={queries}>
      <DecisionCard decision={decision} where="inbox" parksAt={null} />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), client: queries, view };
}

function posts() {
  return api.seen.filter((request) => request.method === "POST" && !request.url.endsWith("/csrf"));
}

function valueField() {
  return screen.getByTestId("credential-value") as HTMLInputElement;
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
  sonner.dismiss();
});

describe("credential-model", () => {
  it("refuses a value as the hub does: none for an option that gives it, too long, a NUL, inside the words", () => {
    expect(valueProblem("value", "", "")).toBe("missing");
    expect(valueProblem("both", "x".repeat(MAX_SECRET_BYTES + 1), "")).toBe("long");
    expect(valueProblem("value", "é".repeat(MAX_SECRET_BYTES / 2 + 1), "")).toBe("long");
    expect(valueProblem("value", "a\u0000b", "")).toBe("nul");
    expect(valueProblem("value", SECRET, `use ${SECRET} please`)).toBe("inWords");
    expect(valueProblem("value", SECRET, "use the staging key")).toBeNull();
    expect(valueProblem("as_me", "", "")).toBeNull();
    expect(valueProblem("refuse", "", "")).toBeNull();
  });

  it("opens a request for a variable on the value, and one for the account alone on nothing", () => {
    expect(credentialOption(CREDENTIAL)).toBe("value");
    expect(credentialOption({ ...CREDENTIAL, credential_request: { env_var: null, secret_name: null, hub_account: true } })).toBeNull();
    expect(credentialOption({ ...CREDENTIAL, category: "scope" })).toBeNull();
    expect([sendWords("value"), sendWords("as_me"), sendWords("both"), sendWords("refuse"), sendWords(null)]).toEqual([
      "sendValue",
      "sendAccount",
      "sendBoth",
      "sendRefuse",
      null,
    ]);
    expect([footWords("value"), footWords("as_me"), footWords("both"), footWords("refuse")]).toEqual(["footValue", "footAccount", "footBoth", null]);
  });
});

describe("a credential request on the DecisionCard", () => {
  it("says what the run asks for among the grounds, to the owner and to anyone else", () => {
    const first = setup();
    expect(screen.getByTestId("decision-credential-needs")).toHaveTextContent(
      "Run xin NORTHWIND_DEPLOY_KEY, để lưu thành secret northwind-deploy-key của octo, và xin dùng tài khoản hub của octo.",
    );
    first.view.unmount();

    setup(CREDENTIAL, "someone-else");
    expect(screen.getByTestId("decision-credential-needs")).toBeInTheDocument();
    expect(screen.getAllByTestId("decision-option").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("credential-value")).toBeNull();
    expect(screen.queryByTestId("decision-form")).toBeNull();
  });

  it("opens on the value with an empty password field the page never fills, which no form submission names", () => {
    setup();
    expect(screen.getByRole("radio", { name: new RegExp(`^Paste the value of ${VARIABLE}$`) })).toBeChecked();
    const field = valueField();
    expect(field).toHaveAttribute("type", "password");
    expect(field.value).toBe("");
    expect(field).not.toHaveAttribute("name");
    expect(field).toHaveAttribute("autocomplete", "off");
    expect(field).toHaveAccessibleName("Giá trị của NORTHWIND_DEPLOY_KEY");
    expect(screen.getByTestId("credential-value-hint")).toHaveTextContent("Lưu thành secret northwind-deploy-key của bạn cho dự án northwind.");
    expect(screen.getByTestId("decision-send")).toHaveTextContent("Gửi giá trị");
    expect(screen.getByTestId("decision-form-hint")).toHaveTextContent("Giá trị vào secrets của bạn, không bao giờ nằm trong câu trả lời mà agent đọc.");
    expect(screen.queryByTestId("credential-account-note")).toBeNull();
  });

  it("sends the value with its option, empties the field as the answer leaves, and keeps it in no cache or page", async () => {
    let release: (response: Response) => void = () => undefined;
    api.answer = () => new Promise<Response>((resolve) => (release = resolve));
    const { user, client: queries } = setup();
    await user.type(valueField(), SECRET);
    await user.type(screen.getByTestId("decision-text"), "the staging key");
    await user.click(screen.getByTestId("decision-send"));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(valueField().value, "emptied before the hub answers").toBe("");
    expect(screen.getByTestId("decision-send")).toHaveAttribute("aria-busy", "true");
    expect(JSON.parse(posts()[0].body)).toEqual({ option: "value", text: "the staging key", secret: SECRET });
    expect(posts()[0].url).toBe("http://hub.test/v1/projects/northwind/decisions/91/answer");
    release(Response.json(ANSWERED));
    await screen.findByText("Đã gửi câu trả lời tới run #320");
    expect(queries.getMutationCache().getAll()).toHaveLength(0);
    expect(JSON.stringify(queries.getQueryCache().getAll().map((query) => query.state.data))).not.toContain(SECRET);
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("empties the field even when the hub refuses, and says to paste the value again", async () => {
    api.answer = () => Response.json({ error: "conflict", message: "secret northwind-deploy-key of octo is a git secret", request_id: "rid-9" }, { status: 409 });
    const { user } = setup();
    await user.type(valueField(), SECRET);
    await user.click(screen.getByTestId("decision-send"));
    await screen.findByText("Không gửi được câu trả lời");
    expect(valueField().value).toBe("");
    expect(await screen.findByTestId("credential-value-forgotten")).toHaveTextContent("Ô đã được xoá khi bạn gửi. Hãy dán lại giá trị để trả lời.");
    expect(document.body.innerHTML).not.toContain(SECRET);
    await user.type(valueField(), "a");
    expect(screen.queryByTestId("credential-value-forgotten")).toBeNull();
  });

  it("refuses a missing value and a value inside the note beside the field, without asking the hub", async () => {
    const { user } = setup();
    await user.click(screen.getByTestId("decision-send"));
    expect(screen.getByTestId("credential-value-problem")).toHaveTextContent("Hãy dán giá trị của NORTHWIND_DEPLOY_KEY, hoặc chọn phương án khác.");
    expect(valueField()).toHaveFocus();
    expect(valueField()).toHaveAttribute("aria-invalid", "true");

    await user.type(valueField(), SECRET);
    expect(screen.queryByTestId("credential-value-problem")).toBeNull();
    await user.type(screen.getByTestId("decision-text"), `here: ${SECRET}`);
    await user.click(screen.getByTestId("decision-send"));
    expect(screen.getByTestId("credential-value-problem")).toHaveTextContent("Ghi chú của bạn chứa giá trị này");
    expect(valueField().value, "nothing was sent, so the field keeps what was pasted").toBe(SECRET);
    expect(api.seen).toHaveLength(0);
  });

  it("lets the run use the owner's account: no value field, what it lets the agent do, and a button that says so", async () => {
    api.answer = () => Response.json({ ...ANSWERED, answer_option: "as_me" });
    const { user } = setup();
    await user.click(screen.getByRole("radio", { name: /^Let the run use my hub account/ }));
    expect(screen.queryByTestId("credential-value")).toBeNull();
    expect(screen.getByTestId("credential-account-note")).toHaveTextContent("không bao giờ dùng secrets, tokens hay workers của bạn");
    expect(screen.getByTestId("decision-form-hint")).toHaveTextContent("Run chạy như bạn ngay khi bạn trả lời");
    const send = screen.getByTestId("decision-send");
    expect(send).toHaveTextContent("Cho dùng tài khoản của tôi");
    await user.click(send);
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(JSON.parse(posts()[0].body)).toEqual({ option: "as_me" });
  });

  it("asks for the value and warns of the account for both, and a value typed for one option is gone with it", async () => {
    const { user } = setup();
    await user.type(valueField(), SECRET);
    await user.click(screen.getByRole("radio", { name: /^Refuse/ }));
    expect(screen.queryByTestId("credential-value")).toBeNull();
    expect(screen.getByTestId("decision-send")).toHaveTextContent("Từ chối");
    await user.click(screen.getByRole("radio", { name: /and let the run use my hub account/ }));
    expect(valueField().value).toBe("");
    expect(screen.getByTestId("credential-account-note")).toBeInTheDocument();
    expect(screen.getByTestId("decision-send")).toHaveTextContent("Gửi giá trị và cho dùng tài khoản");
  });

  it("opens a request for the account alone with nothing picked, so the owner picks it on purpose", () => {
    setup({ ...CREDENTIAL, options: OPTIONS.slice(2), credential_request: { env_var: null, secret_name: null, hub_account: true } });
    expect(screen.getByTestId("decision-credential-needs")).toHaveTextContent("Run xin dùng tài khoản hub của octo");
    expect(screen.getAllByRole("radio").filter((radio) => (radio as HTMLInputElement).checked)).toHaveLength(0);
    expect(screen.queryByTestId("credential-value")).toBeNull();
    expect(screen.getByTestId("decision-send")).toHaveTextContent("Gửi câu trả lời");
  });

  it("shows an answered request as any answered decision: the option and the hub's sentence, never a field", () => {
    setup(ANSWERED);
    expect(screen.queryByTestId("credential-value")).toBeNull();
    expect(within(screen.getByTestId("decision-answer")).getByTestId("decision-answer-text")).toHaveTextContent("northwind-deploy-key");
  });
});

function PhoneForm({ decision }: { decision: Decision }) {
  const form = useAnswerForm(decision);
  return (
    <form {...form.formProps}>
      <AnswerFields form={form} screen />
    </form>
  );
}

describe("a credential request on the phone's decision screen", () => {
  it("gives the value field 16 px text and a 44 px height, so the phone does not zoom into it", () => {
    renderVi(
      <QueryClientProvider client={client("octo")}>
        <PhoneForm decision={CREDENTIAL} />
      </QueryClientProvider>,
    );
    expect(valueField()).toHaveClass("h-11", "text-base");
  });
});
