// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { OpsTag } from "./ops-tag";
import type { Notification } from "./queries";

/** A notification the Orchestrator of the default system holds says so, and links to its thread once it opened. */

const held = (ops: Notification["ops"]) => ({ title: "Run failed", ops }) as Pick<Notification, "ops" | "title">;

describe("the ops tag of a notification", () => {
  it("links to the thread the Orchestrator works in", () => {
    renderVi(<OpsTag notification={held({ batch_id: 4, state: "running", thread: "/chats/7?thread=31" })} />);
    expect(screen.getByTestId("notification-ops")).toHaveAttribute("data-state", "running");
    expect(screen.getByTestId("notification-ops-thread")).toHaveAttribute("href", "/chats/7?thread=31");
  });

  it("says queued, with no link, before the batch's thread opened", () => {
    renderVi(<OpsTag notification={held({ batch_id: 4, state: "ready", thread: null })} />);
    expect(screen.getByTestId("notification-ops")).toHaveAttribute("data-state", "ready");
    expect(screen.queryByTestId("notification-ops-thread")).toBeNull();
  });

  it("shows nothing for a notification that waits on a person", () => {
    renderVi(<OpsTag notification={held(null)} />);
    expect(screen.queryByTestId("notification-ops")).toBeNull();
  });
});
