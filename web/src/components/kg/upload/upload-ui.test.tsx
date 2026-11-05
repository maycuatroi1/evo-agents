// @vitest-environment jsdom
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import type { KgUpload, KgUploadStarted } from "@/lib/kg/upload";
import { initRunFor, type KgInitRun } from "@/lib/kg/upload-queries";

import messages from "../../../../messages/en.json";
import { UploadProgress } from "./upload-progress";
import { UploadReport } from "./upload-report";
import type { UploadPhase } from "./use-upload";

function renderEn(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="en" messages={messages} timeZone="UTC">
      <TooltipProvider>{ui}</TooltipProvider>
    </NextIntlClientProvider>,
  );
}

const upload = (over: Partial<KgUpload> = {}): KgUpload => ({
  id: 4,
  source: "lumen-notes",
  name: "lumen-notes.zip",
  state: "extracted",
  size: 733,
  label: { level: "customer" },
  sender: "ana",
  created_at: "2026-11-01T10:00:00Z",
  uploaded_at: "2026-11-01T10:00:02Z",
  ended_at: "2026-11-01T10:00:05Z",
  files: 3,
  skipped: { unsafe_path: 1 },
  assets: { binary: 1 },
  paths: [
    { path: "../outside.txt", reason: "unsafe_path" },
    { path: "lumen/crest.png", reason: "binary" },
  ],
  replaced_by: null,
  build_id: 12,
  build_status: "done",
  error: null,
  ...over,
});

const initRun = (over: Partial<KgInitRun> = {}): KgInitRun =>
  ({
    id: 1,
    project: "demo",
    via: "first_build",
    state: "done",
    why: [],
    build_id: 12,
    agent_run_id: 31,
    questions: { agent_run_id: 32 },
    ...over,
  }) as KgInitRun;

const inList = (ui: ReactElement) => renderEn(<ul>{ui}</ul>);

describe("UploadReport", () => {
  it("reports what was read and skipped by reason, links the build and the setup runs, and shows no pill", () => {
    inList(
      <UploadReport project="demo" upload={upload()} initRun={initRun()} />,
    );
    const report = screen.getByTestId("kg-upload-report");
    expect(within(report).getByTestId("kg-upload-files")).toHaveTextContent(
      "3",
    );
    expect(within(report).getByTestId("kg-upload-skipped")).toHaveTextContent(
      "Unsafe path: 1",
    );
    expect(within(report).getByTestId("kg-upload-assets")).toHaveTextContent(
      "Binary file: 1",
    );
    expect(within(report).getByTestId("kg-upload-build-link")).toHaveAttribute(
      "href",
      "/p/demo/kg/builds",
    );
    expect(within(report).getByTestId("kg-upload-init-link")).toHaveAttribute(
      "href",
      "/p/demo/runs/31",
    );
    expect(within(report).getByTestId("kg-upload-init-link")).toHaveTextContent(
      "Graph setup run #31",
    );
    expect(
      within(report).getByTestId("kg-upload-questions-link"),
    ).toHaveAttribute("href", "/p/demo/runs/32");
    expect(within(report).getByTestId("kg-upload-paths")).toHaveTextContent(
      "../outside.txt",
    );
    expect(report).toHaveTextContent("Label customer");
    expect(report).toHaveTextContent("upload.lumen-notes");
    expect(within(report).queryByTestId("kg-upload-state")).toBeNull();
    expect(within(report).queryByTestId("kg-upload-build-state")).toBeNull();
    expect(within(report).queryByTestId("kg-upload-moving")).toBeNull();
  });

  it("says in plain words what is still moving, and shows a reason it does not know as the hub sent it", () => {
    inList(
      <UploadReport
        project="demo"
        upload={upload({
          state: "extracting",
          files: null,
          build_id: null,
          paths: [],
        })}
        initRun={null}
      />,
    );
    expect(screen.getByTestId("kg-upload-moving")).toHaveTextContent("Reading");
    expect(screen.queryByTestId("kg-upload-counts")).toBeNull();
    expect(screen.queryByTestId("kg-upload-build-link")).toBeNull();
    expect(screen.queryByTestId("kg-upload-init-link")).toBeNull();
  });

  it("marks a failed upload and a failed build, with the hub's words", () => {
    const { unmount } = inList(
      <UploadReport
        project="demo"
        upload={upload({
          state: "failed",
          error: "not a zip",
          files: null,
          build_id: null,
        })}
        initRun={null}
      />,
    );
    expect(screen.getByTestId("kg-upload-state")).toHaveTextContent("Failed");
    expect(screen.getByTestId("kg-upload-error")).toHaveTextContent(
      "not a zip",
    );
    unmount();
    inList(
      <UploadReport
        project="demo"
        upload={upload({
          build_status: "failed",
          skipped: { future_reason: 2 },
        })}
        initRun={null}
      />,
    );
    expect(screen.getByTestId("kg-upload-build-state")).toHaveTextContent(
      "Build failed",
    );
    expect(screen.getByTestId("kg-upload-skipped")).toHaveTextContent(
      "future_reason: 2",
    );
    expect(screen.queryByTestId("kg-upload-state")).toBeNull();
  });
});

const started = (over: Partial<KgUploadStarted> = {}): KgUploadStarted => ({
  id: 7,
  source: "lumen",
  state: "uploading",
  size: 250,
  part_size: 100,
  parts: 3,
  done: [1],
  resumed: true,
  max_size: 1_000,
  ...over,
});

describe("UploadProgress", () => {
  const file = new File([new Uint8Array(250)], "lumen.zip");
  const noop = () => {};

  it("shows nothing before an upload, and a meter for each part while it sends", async () => {
    const { container, unmount } = renderEn(
      <UploadProgress
        state={{ phase: "idle" }}
        onCancel={noop}
        onRetry={noop}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    unmount();
    const cancel = vi.fn();
    const state: UploadPhase = {
      phase: "sending",
      file,
      started: started(),
      sent: { 1: 100, 2: 40 },
    };
    renderEn(<UploadProgress state={state} onCancel={cancel} onRetry={noop} />);
    expect(screen.getByTestId("kg-upload-progress")).toHaveAttribute(
      "data-phase",
      "sending",
    );
    const parts = screen.getAllByTestId("kg-upload-part");
    expect(parts.map((part) => part.getAttribute("data-percent"))).toEqual([
      "100",
      "40",
      "0",
    ]);
    expect(screen.getByTestId("kg-upload-resumed")).toHaveTextContent(
      "1 part was already on the hub",
    );
    await userEvent.click(screen.getByTestId("kg-upload-cancel"));
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("puts a failure in a danger banner with the hub's words and a retry", async () => {
    const retry = vi.fn();
    const state: UploadPhase = {
      phase: "failed",
      file,
      error: {
        status: 0,
        code: "network",
        message: "The part did not reach the hub.",
        requestId: null,
      },
    };
    renderEn(<UploadProgress state={state} onCancel={noop} onRetry={retry} />);
    const banner = screen.getByTestId("kg-upload-failed");
    expect(banner).toHaveTextContent("The upload of lumen.zip stopped");
    expect(banner).toHaveTextContent("The part did not reach the hub.");
    expect(screen.queryByTestId("kg-upload-progress")).toBeNull();
    await userEvent.click(screen.getByTestId("kg-upload-retry"));
    expect(retry).toHaveBeenCalledOnce();
  });
});

describe("initRunFor", () => {
  // Newest first, as GET /v1/kg/{project}/init lists them: #3 asked again after #1 failed, both on the upload's build.
  const failed = initRun({ id: 1, state: "failed", build_id: 12, agent_run_id: 720 });
  const rerun = initRun({ id: 3, state: "done", build_id: 12, agent_run_id: 729, questions: { agent_run_id: 728 } });
  const older = initRun({ id: 0, state: "done", build_id: 9, agent_run_id: 700 });

  it("takes the newest kg_init run on the upload's build or a later one, as the pipeline does", () => {
    expect(initRunFor(upload(), [rerun, failed, older])?.id).toBe(3);
    const later = initRun({ id: 5, state: "running", build_id: 14, agent_run_id: 740 });
    expect(initRunFor(upload(), [later, rerun, failed])?.id).toBe(5);
    expect(initRunFor(upload(), [older])).toBeNull();
    expect(initRunFor(upload({ build_id: null }), [rerun])).toBeNull();
  });

  it("names the pipeline's own run when the pipeline says which, and none when it names none", () => {
    expect(initRunFor(upload(), [rerun, failed], 1)?.agent_run_id).toBe(720);
    expect(initRunFor(upload(), [rerun, failed], null)).toBeNull();
    expect(initRunFor(upload(), [rerun, failed], undefined)?.id).toBe(3);
  });
});
