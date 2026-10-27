// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";
import { clampWidth, THREAD_WIDTH, THREAD_WIDTH_KEY, ThreadResizeHandle, useThreadWidth } from "./thread-width";

function Frame() {
  const frame = useRef<HTMLDivElement | null>(null);
  const { share, resize } = useThreadWidth();
  return (
    <div ref={frame}>
      <ThreadResizeHandle frame={frame} share={share} onResize={resize} />
    </div>
  );
}

afterEach(() => window.localStorage.clear());

describe("thread width", () => {
  it("keeps the share between its bounds", () => {
    expect(clampWidth(0.1)).toBe(THREAD_WIDTH.min);
    expect(clampWidth(0.95)).toBe(THREAD_WIDTH.max);
    expect(clampWidth(0.5)).toBe(0.5);
  });

  it("widens with Left, narrows with Right, and keeps the width for the next thread", () => {
    renderVi(<Frame />);
    const bar = screen.getByRole("separator", { name: messages.chats.thread.resize });
    expect(bar).toHaveAttribute("aria-valuenow", "45");
    fireEvent.keyDown(bar, { key: "ArrowLeft" });
    expect(bar).toHaveAttribute("aria-valuenow", "50");
    fireEvent.keyDown(bar, { key: "End" });
    expect(bar).toHaveAttribute("aria-valuenow", "70");
    fireEvent.keyDown(bar, { key: "ArrowLeft" });
    expect(bar).toHaveAttribute("aria-valuenow", "70");
    fireEvent.keyDown(bar, { key: "ArrowRight" });
    expect(window.localStorage.getItem(THREAD_WIDTH_KEY)).toBe("0.650");
  });

  it("opens at the width kept, and a double click puts the default back", () => {
    window.localStorage.setItem(THREAD_WIDTH_KEY, "0.6");
    renderVi(<Frame />);
    const bar = screen.getByRole("separator");
    expect(bar).toHaveAttribute("aria-valuenow", "60");
    fireEvent.doubleClick(bar);
    expect(bar).toHaveAttribute("aria-valuenow", "45");
  });
});
