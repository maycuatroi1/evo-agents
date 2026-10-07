// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import codex from "@/test/fixtures/trace/codex.json";
import opencode from "@/test/fixtures/trace/opencode.json";
import { renderVi } from "@/test/render";

import type { RunMove } from "./log-model";
import type { Run, RunEvent } from "./queries";
import { RunTimeline } from "./run-timeline";
import { UsageMeter } from "./usage-meter";

/** The run page's timeline and usage card, as the side column and the head of the page show them. */

const run = {
  id: 12,
  state: "running",
  approval: "auto",
  runtime: "opencode",
  usage: null,
  queued_at: "2026-10-07T03:00:00Z",
  leased_at: "2026-10-07T03:00:01Z",
  started_at: "2026-10-07T03:00:09Z",
  waiting_since: null,
  parked_at: null,
  finished_at: null,
} as unknown as Run;

describe("UsageMeter", () => {
  it("adds up a running run's reports so far into the total, the bar and the rows, with the cost as reported", () => {
    renderVi(<UsageMeter run={run} events={opencode.events as RunEvent[]} />);
    const card = screen.getByTestId("run-usage");
    expect(card).toHaveAttribute("data-source", "events");
    expect(card).toHaveTextContent("đến giờ");
    expect(within(card).getByTestId("run-usage-total")).toHaveTextContent("378.932");
    expect(within(card).getByTestId("run-usage-cost")).toHaveTextContent("theo runtime báo");
    expect(within(card).getByRole("img")).toHaveAccessibleName("Đọc cache 74,9 phần trăm, input 25,1, output dưới 0,1, suy luận dưới 0,1");
    const rows = within(card).getByTestId("run-usage-rows");
    expect(rows).toHaveTextContent("Đọc cache283.77674,9%");
    // A part that rounds to 0.0 % says it is under 0.1 %.
    expect(rows).toHaveTextContent("Suy luận109dưới 0,1%");
    expect(within(card).getByTestId("run-usage-note")).toHaveTextContent("Ghi cache 0. Chi phí đúng như runtime báo.");
  });

  it("says when the runtime reported no cost, and prefers the run's own usage once it ended", () => {
    const ended = { ...run, state: "done", runtime: "codex", usage: codex.usage } as unknown as Run;
    renderVi(<UsageMeter run={ended} events={[]} />);
    const card = screen.getByTestId("run-usage");
    expect(card).toHaveAttribute("data-source", "run");
    expect(card).toHaveTextContent("Codex CLI");
    expect(within(card).getByTestId("run-usage-total")).toHaveTextContent("125.228");
    expect(within(card).getByTestId("run-usage-cost")).toHaveTextContent("Runtime không báo chi phí");
  });

  it("waits for the first report, and says when none came", () => {
    const first = renderVi(<UsageMeter run={run} events={[]} />);
    expect(screen.getByTestId("run-usage-empty")).toHaveTextContent("Hiện khi agent xong lượt đầu tiên");
    first.unmount();
    renderVi(<UsageMeter run={{ ...run, state: "failed" } as Run} events={[]} />);
    expect(screen.getByTestId("run-usage-empty")).toHaveTextContent("Runtime không báo mức dùng của run này.");
  });
});

describe("RunTimeline", () => {
  const show = (value: Run, moves: RunMove[] = []) =>
    renderVi(
      <TooltipProvider>
        <RunTimeline run={value} moves={moves} />
      </TooltipProvider>,
    );
  const phase = (name: string) => screen.getAllByTestId("run-phase").find((node) => node.getAttribute("data-phase") === name)!;

  it("is an ordered list of the run's phases, the current one marked, with the time between them on the line", () => {
    show(run);
    const list = screen.getByRole("list", { name: "Các pha của run #12" });
    expect(within(list).getAllByTestId("run-phase").map((node) => node.getAttribute("data-phase"))).toEqual(["queued", "leased", "running", "verifying", "done"]);
    expect(phase("running")).toHaveAttribute("aria-current", "step");
    expect(phase("running")).toHaveAttribute("data-tone", "current");
    expect(within(phase("queued")).getByTestId("run-phase-gap")).toHaveTextContent("1 giây");
    expect(within(phase("leased")).getByTestId("run-phase-gap")).toHaveTextContent("8 giây");
    expect(within(phase("queued")).getByTestId("run-phase-time")).toHaveAttribute("dateTime", run.queued_at);
    expect(phase("verifying")).toHaveTextContent("chưa tới");
  });

  it("shows a waiting run's state since when, in attention, and a failed run where it stopped", () => {
    const first = show({ ...run, state: "waiting", waiting_since: "2026-10-07T03:05:00Z" } as Run);
    expect(phase("running")).toHaveAttribute("data-tone", "waiting");
    expect(within(phase("running")).getByTestId("run-phase-name")).toHaveTextContent("Chờ quyết định");
    expect(within(phase("running")).getByTestId("run-phase-time")).toHaveTextContent(/^từ /);
    first.unmount();

    show({ ...run, state: "failed", finished_at: "2026-10-07T03:20:09Z" } as Run, [
      { seq: 9, at: "2026-10-07T03:20:09Z", from: "running", to: "failed", actor: "worker", reason: null },
    ]);
    expect(phase("running")).toHaveAttribute("data-tone", "failed");
    expect(within(phase("running")).getByTestId("run-phase-name")).toHaveTextContent("Thất bại");
    expect(within(phase("leased")).getByTestId("run-phase-gap")).toHaveTextContent("20 phút");
    expect(phase("done")).toHaveTextContent("bỏ qua");
  });
});
