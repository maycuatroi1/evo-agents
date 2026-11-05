// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import type { KgPipeline, KgPipelinePhase } from "@/lib/kg/upload-queries";

import messages from "../../../../messages/en.json";
import { PipelineView } from "./upload-pipeline";

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages} timeZone="UTC">
      {ui}
    </NextIntlClientProvider>,
  );
}

const phase = (name: KgPipelinePhase["name"], state: KgPipelinePhase["state"], detail: string | null = null): KgPipelinePhase => ({
  name,
  state,
  detail,
});

const pipeline = (over: Partial<KgPipeline> = {}): KgPipeline => ({
  upload_id: 4,
  project: "demo",
  state: "running",
  phase: "building",
  phases: [
    phase("uploading", "done"),
    phase("extracting", "done", "3 files"),
    phase("building", "running", "build #12 running"),
    phase("entities", "pending"),
    phase("questions", "pending"),
    phase("curator", "pending"),
  ],
  error: null,
  build_id: 12,
  init_id: null,
  rebuild_id: null,
  told_at: null,
  ...over,
});

describe("PipelineView", () => {
  it("says the build is in progress with the phase, and lists each phase with its state and detail", () => {
    renderEn(<PipelineView project="demo" pipeline={pipeline()} />);
    const view = screen.getByTestId("kg-upload-pipeline");
    expect(view).toHaveAttribute("data-state", "running");
    expect(within(view).getByTestId("kg-upload-pipeline-headline")).toHaveTextContent("Knowledge graph build in progress: Build the graph");
    const phases = within(view).getAllByTestId("kg-upload-pipeline-phase");
    expect(phases.map((item) => item.getAttribute("data-phase"))).toEqual(["uploading", "extracting", "building", "entities", "questions", "curator"]);
    expect(phases[2]).toHaveAttribute("data-state", "running");
    for (const words of ["Build the graph", "Running", "build #12 running"]) expect(phases[2]).toHaveTextContent(words);
    expect(phases[1]).toHaveTextContent("3 files");
    expect(within(view).queryByTestId("kg-upload-pipeline-open")).toBeNull();
  });

  it("says the graph is ready and links its page", () => {
    const done = ["uploading", "extracting", "building", "questions", "curator", "entities"] as const;
    renderEn(<PipelineView project="demo" pipeline={pipeline({ state: "ready", phase: null, phases: done.map((name) => phase(name, "done")) })} />);
    const view = screen.getByTestId("kg-upload-pipeline");
    expect(view).toHaveAttribute("data-state", "ready");
    expect(within(view).getByTestId("kg-upload-pipeline-headline")).toHaveTextContent("Knowledge graph ready");
    expect(within(view).getByTestId("kg-upload-pipeline-open")).toHaveAttribute("href", "/p/demo/kg");
  });

  it("names the failed phase and the hub's reason", () => {
    const failed = pipeline({
      state: "failed",
      phase: "entities",
      error: "no key: set OPENROUTER_API_KEY",
      phases: [phase("uploading", "done"), phase("building", "done"), phase("entities", "failed", "no key: set OPENROUTER_API_KEY")],
    });
    renderEn(<PipelineView project="demo" pipeline={failed} />);
    const view = screen.getByTestId("kg-upload-pipeline");
    expect(within(view).getByTestId("kg-upload-pipeline-headline")).toHaveTextContent("Knowledge graph build failed at Entities");
    expect(within(view).getByTestId("kg-upload-pipeline-error")).toHaveTextContent("Reason: no key: set OPENROUTER_API_KEY");
    expect(within(view).queryByTestId("kg-upload-pipeline-open")).toBeNull();
  });
});
