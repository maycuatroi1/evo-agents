// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { CYCLE, SCHEDULE, STATUS, TAG } from "@/test/fixtures/releases";

import en from "../../../messages/en.json";

import { ActionsBanner, CycleCard, NoCycle, StoppedBanner } from "./cycle-card";

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={en} timeZone="Asia/Ho_Chi_Minh">
      <TooltipProvider>{ui}</TooltipProvider>
    </NextIntlClientProvider>,
  );
}

describe("CycleCard", () => {
  it("shows the cycle's range, its cost, its tags with their CI and its runs with their pages", () => {
    renderEn(<CycleCard project="fleet" cycle={CYCLE} schedule={SCHEDULE} />);
    const card = screen.getByTestId("release-cycle");
    expect(within(card).getByRole("heading", { level: 2 })).toHaveTextContent("Cycle #7");
    expect(within(card).queryByTestId("release-cycle-state")).toBeNull(); // an open cycle carries no pill
    expect(screen.getByTestId("release-cycle-start")).toHaveTextContent("A cut of nightly");
    expect(screen.getByTestId("release-cycle-cost")).toHaveTextContent("$12.40");
    expect(screen.queryByRole("progressbar")).toBeNull(); // a cycle has no cap to measure against
    expect(within(screen.getByTestId("release-cycle-paths")).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["evo_agents/", "web/"]);

    const tags = screen.getByTestId("release-tags");
    expect(within(tags).getByTestId("release-ci")).toHaveTextContent("Failed");
    const tag = within(tags).getByTestId("release-tag");
    expect(tag).toHaveAttribute("href", "https://github.com/northwind/fleet/tree/v0.25.0");
    expect(tag).toHaveAccessibleName("Tag v0.25.0 on GitHub (opens in a new tab)");
    const workflow = within(tags).getByTestId("release-workflow-link");
    expect(workflow).toHaveAttribute("href", TAG.workflow_url);
    expect(workflow).toHaveAttribute("target", "_blank");
    expect(workflow).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(tags).getByTestId("release-failed-tests")).toHaveTextContent(TAG.failed_tests[0]);

    const runs = screen.getByTestId("release-runs");
    const links = within(runs).getAllByTestId("release-run-link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/p/fleet/runs/41", "/p/fleet/runs/44"]);
    expect(within(runs).getAllByTestId("release-run-mode").map((mode) => mode.textContent)).toEqual(["Cut", "Fix of v0.25.0"]);
    expect(within(runs).getAllByTestId("run-state").map((state) => state.getAttribute("data-status"))).toEqual(["done", "running"]);
    expect(within(runs).getByTestId("release-pull-link")).toHaveAttribute("href", "https://github.com/northwind/fleet/pull/12");
  });

  it("refuses a workflow address that is not http or https", () => {
    const tag = { ...TAG, workflow_url: "javascript:alert(1)" };
    renderEn(<CycleCard project="fleet" cycle={{ ...CYCLE, tags: [tag] }} schedule={SCHEDULE} />);
    expect(screen.queryByTestId("release-workflow-link")).toBeNull();
    expect(screen.getByTestId("release-tags")).toHaveTextContent("No run yet");
  });

  it("says a person tagged a tag no run made", () => {
    renderEn(<CycleCard project="fleet" cycle={{ ...CYCLE, tags: [{ ...TAG, run_id: null, ci_state: "running", failed_tests: [] }] }} schedule={SCHEDULE} />);
    const tags = screen.getByTestId("release-tags");
    expect(tags).toHaveTextContent("A person");
    expect(within(tags).getByTestId("release-ci")).toHaveTextContent("Running");
    expect(within(tags).queryByTestId("release-tag-detail")).toBeNull();
  });
});

describe("the release's problems", () => {
  it("says why a stopped cycle stopped, what it waits for, and links its decision and triage thread", () => {
    const stopped = {
      ...CYCLE,
      state: "stopped" as const,
      stop_cause: "same_test" as const,
      stop_reason: "tests/hub/test_paths.py::test_a failed at v0.25.0 and again at v0.25.1",
      decision_id: 31,
      thread: "/chats/5?message=88",
    };
    renderEn(<StoppedBanner cycle={stopped} />);
    const banner = screen.getByTestId("release-stopped");
    expect(banner).toHaveTextContent("Cycle #7 stopped: the same test failed at two tags in a row");
    expect(banner).toHaveTextContent(stopped.stop_reason);
    expect(banner).toHaveTextContent("It moves no further until octo answers");
    expect(screen.getByTestId("release-decision-link")).toHaveAttribute("href", "/inbox?decision=31");
    expect(screen.getByTestId("release-thread-link")).toHaveAttribute("href", "/chats/5?message=88");
  });

  it("says nothing of an open cycle", () => {
    const { container } = renderEn(<StoppedBanner cycle={CYCLE} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("says the App's installation grants no Actions as an alert", () => {
    renderEn(<ActionsBanner schedule={SCHEDULE} />);
    const banner = screen.getByRole("alert");
    expect(banner).toHaveTextContent("The hub cannot read the CI of release.yml");
    expect(banner).toHaveTextContent("on northwind/fleet does not grant Actions");
  });

  it("says no cycle is open in one quiet line, and that a paused schedule opens none", () => {
    renderEn(<NoCycle status={{ ...STATUS, cycle: null, paused: true }} />);
    expect(screen.getByTestId("release-no-cycle")).toHaveTextContent("No cycle open. The schedule is paused: no check opens one.");
  });
});
