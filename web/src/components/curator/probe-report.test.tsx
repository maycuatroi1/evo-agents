// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import en from "../../../messages/en.json";

import { ProbeCard } from "./probe-card";
import { flaggedCounts, flaggedTotal, isFlagged, reportShown, taskLine, weeksOfProbe } from "./probe-model";
import { type Probe, probeKeys, PROBE_WEEKS, type ProbeWeek, type ProbeWeekly, type ProbeWeekResult } from "./probe-queries";
import { ProbeReport } from "./probe-report";

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async () => Response.json({ error: "not_found", message: "no such thing" }, { status: 404 }),
    }),
}));

const ARM_NONE = { runs: 0, failed: 0, recurrence: null };

function result(probe: number, status: ProbeWeekResult["status"], extra: Partial<ProbeWeekResult> = {}): ProbeWeekResult {
  return {
    change_id: 40 + probe,
    probe_id: probe,
    runtime: "claude-code",
    model: "claude-opus-5-5",
    k: 3,
    on: { runs: 3, failed: status === "recurring" ? 2 : 0, recurrence: status === "recurring" ? 0.963 : 0 },
    off: { runs: 3, failed: status === "not_reproduced" ? 0 : 3, recurrence: status === "not_reproduced" ? 0 : 1 },
    status,
    previous: null,
    reason: null,
    run_id: 90 + probe,
    ...extra,
  };
}

function week(day: string, complete: boolean, probes: ProbeWeekResult[]): ProbeWeek {
  const count = (status: ProbeWeekResult["status"]) => probes.filter((item) => item.status === status).length;
  return {
    week: day,
    complete,
    cost_usd: 1.2,
    budget_usd: 5,
    probes,
    totals: {
      probes: probes.length,
      holds: count("holds"),
      recurring: count("recurring"),
      not_reproduced: count("not_reproduced"),
      stale: count("stale"),
      unmeasured: count("unmeasured"),
      on: ARM_NONE,
      off: ARM_NONE,
    },
  };
}

const REPORT: ProbeWeekly = {
  project: "demo",
  k: 3,
  budget_usd: 5,
  weeks: [
    week("2026-10-19", false, [
      result(1, "recurring", { previous: "holds" }),
      result(2, "holds"),
      result(3, "unmeasured", { on: ARM_NONE, off: ARM_NONE, k: null, reason: "not measured yet this week", run_id: null }),
    ]),
    week("2026-10-12", true, [result(1, "holds"), result(2, "holds")]),
  ],
  unprobed: [{ change_id: 50, proposal_id: 20, title: "Ask before a force-push", merged_at: "2026-10-10T03:00:00Z", reason: "no probe was written for it" }],
  fixes: [{ proposal_id: 30, change_id: 41, title: "Tighten the wait rule", state: "open" }],
  replacements: [],
};

const PROBE: Probe = {
  id: 1,
  project: "demo",
  proposal_id: 12,
  change_id: 41,
  repo: "api",
  commit: "abc1234",
  task: "Wait for the deploy to finish\nthen report what changed.",
  verify: "test ! -e used-sleep.txt",
  state: "active",
  source: "proposal",
  created_by: null,
  created_run_id: 7,
  retired_at: null,
  retired_note: null,
  created_at: "2026-10-01T01:00:00Z",
  updated_at: "2026-10-01T01:00:00Z",
  replaces: null,
};

function withData(ui: ReactElement, report: ProbeWeekly, probes: Probe[] = [PROBE]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(probeKeys.report("demo", PROBE_WEEKS), report);
  client.setQueryData(probeKeys.list("demo", null), { probes, total: probes.length });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={en} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
  );
}

describe("the probe report's model", () => {
  it("flags every status but holds, and counts a week's flags by status", () => {
    expect(isFlagged("holds")).toBe(false);
    expect(["recurring", "not_reproduced", "stale", "unmeasured"].every((status) => isFlagged(status as ProbeWeekResult["status"]))).toBe(true);
    const latest = REPORT.weeks[0]!;
    expect(flaggedCounts(latest)).toEqual([
      { status: "recurring", count: 1 },
      { status: "unmeasured", count: 1 },
    ]);
    expect(flaggedTotal(latest)).toBe(2);
    expect(flaggedTotal(REPORT.weeks[1]!)).toBe(0);
  });

  it("finds a probe's weeks, names a probe by its task's first line, and shows nothing for a project without probes", () => {
    expect(weeksOfProbe(REPORT, 3).map((item) => item.week.week)).toEqual(["2026-10-19"]);
    expect(weeksOfProbe(REPORT, 1).map((item) => item.results[0]?.status)).toEqual(["recurring", "holds"]);
    expect(taskLine("  First line\nsecond")).toBe("First line");
    expect(taskLine("x".repeat(200), 10)).toBe(`${"x".repeat(9)}…`);
    const quiet = { ...REPORT, budget_usd: null, weeks: REPORT.weeks.map((item) => ({ ...item, probes: [] })), unprobed: [], fixes: [] };
    expect(reportShown(quiet)).toBe(false);
    expect(reportShown({ ...quiet, unprobed: REPORT.unprobed })).toBe(true);
  });
});

describe("ProbeReport", () => {
  it("shows the latest week with a flag only on what is not as expected, and the weeks before it on demand", () => {
    renderEn(withData(<ProbeReport project="demo" />, REPORT));
    const panel = screen.getByTestId("probe-report");
    expect(within(panel).getByRole("heading", { name: "Probes" })).toBeInTheDocument();
    expect(panel).toHaveTextContent("3 sessions an arm on each model, up to $5.00 a week");
    const weeks = within(panel).getAllByTestId("probe-week");
    expect(weeks.map((item) => item.getAttribute("aria-pressed"))).toEqual(["true", "false"]);
    expect(weeks[0]).toHaveTextContent("This week");
    expect(within(weeks[0]!).getByTestId("probe-week-flags")).toHaveTextContent("2 results flagged");
    expect(within(weeks[1]!).queryByTestId("probe-week-flags")).toBeNull();
    expect(within(panel).getByTestId("probe-week-summary")).toHaveTextContent("3 results1 recurring1 not measured$1.20 of $5.00 spent");

    const rows = within(panel).getAllByTestId("probe-result");
    expect(rows).toHaveLength(3);
    const [recurring, holds, unmeasured] = rows as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(recurring).getByTestId("probe-flag")).toHaveTextContent("Recurring");
    expect(within(recurring).getByTestId("probe-previous")).toHaveTextContent("Last week: held");
    expect(within(recurring).getByTestId("probe-proposal-link")).toHaveAttribute("href", "/p/demo/curator/proposals/12");
    expect(recurring).toHaveTextContent("Wait for the deploy to finish");
    expect(recurring).toHaveTextContent("2 of 3 failed");
    expect(within(holds).queryByTestId("probe-flag")).toBeNull();
    expect(holds).not.toHaveTextContent(/holds|held|ok/i);
    expect(holds).toHaveTextContent("Probe #2");
    expect(within(unmeasured).getByTestId("probe-flag")).toHaveTextContent("Not measured");
    expect(within(unmeasured).getByTestId("probe-reason")).toHaveTextContent("not measured yet this week");
    expect(unmeasured).toHaveTextContent("Not run");

    expect(within(panel).getByTestId("probe-fix")).toHaveTextContent("#30 Tighten the wait rule");
    expect(within(panel).getByTestId("probe-unprobed")).toHaveTextContent("no probe was written for it");

    fireEvent.click(weeks[1]!);
    expect(within(panel).getAllByTestId("probe-week").map((item) => item.getAttribute("aria-pressed"))).toEqual(["false", "true"]);
    expect(within(panel).getAllByTestId("probe-result")).toHaveLength(2);
    expect(within(panel).queryAllByTestId("probe-flag")).toHaveLength(0);
  });

  it("shows nothing for a project whose charter runs no probe and that has nothing to list", () => {
    const quiet = { ...REPORT, budget_usd: null, weeks: REPORT.weeks.map((item) => ({ ...item, budget_usd: null, probes: [] })), unprobed: [], fixes: [] };
    renderEn(withData(<ProbeReport project="demo" />, quiet));
    expect(screen.queryByTestId("probe-report")).toBeNull();
  });
});

describe("ProbeCard", () => {
  it("shows an admin the task, the verify and the probe's weeks, and the fix proposal of its change", () => {
    renderEn(withData(<ProbeCard project="demo" probe={PROBE} />, REPORT));
    const card = screen.getByTestId("proposal-probe");
    expect(card).toHaveTextContent("Probe #1 of change #41");
    expect(within(card).getByTestId("probe-card-task")).toHaveTextContent("Wait for the deploy to finish then report what changed.");
    expect(within(card).getByTestId("probe-card-verify")).toHaveTextContent("test ! -e used-sleep.txt");
    const weeks = within(card).getAllByTestId("probe-card-week");
    expect(weeks).toHaveLength(2);
    expect(within(weeks[0]!).getByTestId("probe-flag")).toHaveTextContent("Recurring");
    expect(within(weeks[1]!).queryByTestId("probe-flag")).toBeNull();
    expect(within(card).getByTestId("probe-card-fixes")).toHaveTextContent("#30 Tighten the wait rule");
  });

  it("tells a reader without the verify, in their language, that only admins read it", () => {
    renderVi(withData(<ProbeCard project="demo" probe={{ ...PROBE, verify: null, id: 9 }} />, REPORT));
    expect(screen.queryByTestId("probe-card-verify")).toBeNull();
    expect(screen.getByTestId("probe-card-verify-hidden")).toHaveTextContent("Chỉ admin của dự án đọc được verify");
    expect(screen.getByTestId("probe-card-no-weeks")).toHaveTextContent("Chưa tuần nào đo probe này.");
  });
});
