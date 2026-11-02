// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { useDispatchedToast, usePlanRunToast } from "./hooks";
import type { Run } from "./queries";

const WARNING = "run #12 is queued, but no worker serves project demo: `evo-agents worker projects --worker laptop --add demo`";

function run(id: number, warning: string | null): Run {
  return { id, project: "demo", plan_id: "rollout", warning } as Run;
}

function Dispatched({ runs }: { runs: Run[] }) {
  const dispatched = useDispatchedToast();
  const planRun = usePlanRunToast();
  return (
    <>
      <button type="button" onClick={() => dispatched(runs)}>
        steps
      </button>
      <button type="button" onClick={() => planRun(runs[0])}>
        plan
      </button>
    </>
  );
}

function shown(runs: Run[]) {
  renderVi(
    <QueryClientProvider client={new QueryClient()}>
      <Dispatched runs={runs} />
    </QueryClientProvider>,
  );
}

describe("the toasts after a dispatch", () => {
  it("add the hub's warning when no worker can take the run", async () => {
    shown([run(12, WARNING), run(13, null)]);
    await userEvent.click(screen.getByRole("button", { name: "steps" }));
    expect(await screen.findByText("Chưa worker nào nhận được run #12")).toBeTruthy();
    expect(screen.getByText(WARNING)).toBeTruthy();
    expect(screen.queryByText("Chưa worker nào nhận được run #13")).toBeNull();
  });

  it("say nothing more when a worker can take it", async () => {
    shown([run(14, null)]);
    await userEvent.click(screen.getByRole("button", { name: "plan" }));
    expect(await screen.findByText("Đã dispatch plan run #14", { exact: false })).toBeTruthy();
    expect(screen.queryByText("Chưa worker nào nhận được run #14")).toBeNull();
  });
});
