// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import claudeCode from "@/test/fixtures/trace/claude-code.json";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";

import type { RunEvent } from "./queries";
import { TracePreview } from "./trace-preview";

function show(events: unknown[], loading: boolean) {
  return renderVi(<TracePreview events={events as RunEvent[]} active={false} loading={loading} onOpen={() => {}} />);
}

describe("TracePreview", () => {
  it("holds the rows' place while the log is first read, rather than saying nothing was recorded", () => {
    show([], true);
    const preview = screen.getByTestId("run-trace-preview");
    expect(preview).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("run-trace-preview-loading")).toBeInTheDocument();
    expect(screen.queryByText(messages.runs.detail.preview.none)).not.toBeInTheDocument();
  });

  it("says nothing was recorded once the log of an ended run is read and empty", () => {
    show([], false);
    expect(screen.getByTestId("run-trace-preview")).not.toHaveAttribute("aria-busy");
    expect(screen.queryByTestId("run-trace-preview-loading")).not.toBeInTheDocument();
    expect(screen.getByText(messages.runs.detail.preview.none)).toBeInTheDocument();
  });

  it("shows the rows it has while the stream is still opening", () => {
    show(claudeCode.events, true);
    expect(screen.queryByTestId("run-trace-preview-loading")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("run-trace-preview-row").length).toBeGreaterThan(0);
  });
});
