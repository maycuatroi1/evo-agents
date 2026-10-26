// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChatProgress } from "@/lib/chats";
import { renderVi } from "@/test/render";

import { MessageProgress } from "./message-progress";

const step = (value: ChatProgress["step"], overrides: Partial<ChatProgress> = {}): ChatProgress => ({
  message_id: 4,
  run_id: 21,
  handle: "reviewer",
  step: value,
  worker: "studio",
  repo: null,
  since: "2026-10-20T08:00:00Z",
  ...overrides,
});

describe("MessageProgress", () => {
  it("says nothing for a message no run is on its way for", () => {
    renderVi(<MessageProgress progress={[]} />);
    expect(screen.queryByTestId("message-progress")).toBeNull();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("says in one polite status per run that the agent got it and what it waits for, with a still dot", () => {
    renderVi(<MessageProgress progress={[step("waiting"), step("inbox", { run_id: 22, handle: "fixer" })]} />);
    const [waiting, inbox] = screen.getAllByRole("status");
    expect(waiting).toHaveTextContent("reviewer đã nhận, đang chờ worker studio");
    expect(inbox).toHaveTextContent("fixer đã nhận, tin nằm trong inbox của lượt đang chạy");
    expect(waiting.querySelector(".animate-live-ping")).toBeNull();
    expect(waiting).toHaveAttribute("data-step", "waiting");
  });

  it("names the step of the setup, the repo it opens, with the running dot", () => {
    renderVi(<MessageProgress progress={[step("opening", { repo: "evo-agents" })]} />);
    const line = screen.getByRole("status");
    expect(line).toHaveTextContent("reviewer đang mở evo-agents");
    expect(line.querySelector(".animate-live-ping")).not.toBeNull();
  });

  it("waits for a worker it does not name when the run has none yet", () => {
    renderVi(<MessageProgress progress={[step("waiting", { worker: null })]} />);
    expect(screen.getByRole("status")).toHaveTextContent("reviewer đã nhận, đang chờ một worker");
  });
});
