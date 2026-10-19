import { describe, expect, it } from "vitest";

import type { GroupMessage } from "@/lib/chats";

import { lastOfRun } from "./use-chat-decisions";

const message = (id: number, run: number | null, kind: "agent" | "person" = "agent", project: string | null = "demo") =>
  ({ id, seq: id, run_id: run, author: { kind, project } }) as unknown as GroupMessage;

describe("lastOfRun", () => {
  it("keeps the last agent message of each run", () => {
    const last = lastOfRun([message(1, 7), message(2, 8), message(3, 7), message(4, null, "person")]);
    expect(last.get(7)?.id).toBe(3);
    expect(last.get(8)?.id).toBe(2);
    expect(last.size).toBe(2);
  });

  it("skips a message without a run or an agent project", () => {
    expect(lastOfRun([message(1, null), message(2, 5, "agent", null), message(3, 5, "person")]).size).toBe(0);
  });
});
