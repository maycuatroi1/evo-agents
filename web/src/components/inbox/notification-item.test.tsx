// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { NotificationItem } from "./notification-item";
import type { Notification } from "./queries";

/** A `run_failed` notice leads to the triage thread the hub opened, or to the run when it opened none. */

const notice = (link: string | null) =>
  ({
    id: 3,
    kind: "notice",
    notice_kind: "run_failed",
    title: "Run failed",
    body: "verify failed",
    project: "demo",
    run_id: 12,
    decision_id: null,
    proposal_id: null,
    link,
    read_at: null,
    created_at: "2026-10-19T03:00:00Z",
    details: null,
  }) as unknown as Notification;

function show(value: Notification) {
  return renderVi(<NotificationItem notification={value} href={"/inbox" as never} selected={false} onOpenDecision={vi.fn()} onRead={vi.fn()} reading={false} />);
}

describe("a run_failed notice", () => {
  it("links to the triage thread", () => {
    show(notice("/chats/5?message=9"));
    expect(screen.getByTestId("notification-run-link")).toHaveAttribute("href", "/chats/5?message=9");
  });

  it("links to the run when the hub opened no thread", () => {
    show(notice("/p/demo/runs/12"));
    expect(screen.getByTestId("notification-run-link")).toHaveAttribute("href", "/p/demo/runs/12");
  });
});
