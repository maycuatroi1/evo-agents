import { describe, expect, it } from "vitest";

import { bodyOf, describeCron, emptyForm, formOf, formProblems } from "./model";
import type { CronJob } from "./queries";

describe("describeCron", () => {
  it("reads the common shapes", () => {
    expect(describeCron("* * * * *")).toEqual({ key: "everyMinute" });
    expect(describeCron("*/15 * * * *")).toEqual({ key: "everyMinutes", n: 15 });
    expect(describeCron("5 * * * *")).toEqual({ key: "hourly", minute: 5 });
    expect(describeCron("0 */6 * * *")).toEqual({ key: "everyHours", n: 6, minute: 0 });
    expect(describeCron("30 2 * * *")).toEqual({ key: "daily", times: ["02:30"] });
    expect(describeCron("0 9,17 * * *")).toEqual({ key: "daily", times: ["09:00", "17:00"] });
    expect(describeCron("0 9 * * 1-5")).toEqual({ key: "weekdays", times: ["09:00"] });
    expect(describeCron("0 9 * * 0-6")).toEqual({ key: "daily", times: ["09:00"] });
    expect(describeCron("0 8 1 * *")).toEqual({ key: "monthly", day: 1, times: ["08:00"] });
  });

  it("puts Sunday last and reads 7 as Sunday", () => {
    expect(describeCron("0 10 * * 7,1")).toEqual({ key: "weekly", days: [1, 0], times: ["10:00"] });
    expect(describeCron("0 10 * * 0,3")).toEqual({ key: "weekly", days: [3, 0], times: ["10:00"] });
  });

  it("leaves what it does not read to the expression", () => {
    for (const expression of ["0 9 * 1 *", "0 9-17 * * *", "0 9 */2 * *", "0 9 * * MON", "0 9 1 * 1", "0 0 * *", "0,30 * * * *"]) {
      expect(describeCron(expression)).toBeNull();
    }
  });
});

const job: CronJob = {
  project: "northwind",
  name: "nightly-tests",
  cron: "0 2 * * *",
  timezone: "Asia/Ho_Chi_Minh",
  kind: "command",
  agent: null,
  chat_id: null,
  prompt: null,
  plan: null,
  command: "make test",
  repo: "lumen-api",
  timeout_minutes: 45,
  owner: "ana",
  revision: 3,
  paused: false,
  paused_at: null,
  paused_by: null,
  paused_reason: null,
  next_fire_at: null,
  last_fire: null,
  cost_24h_usd: 0,
  can_change: true,
  can_manage: true,
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
  updated_by: null,
};

describe("the form", () => {
  it("sends only the fields of its kind, at the revision it read", () => {
    expect(bodyOf(formOf(job), job.revision)).toEqual({
      cron: "0 2 * * *",
      timezone: "Asia/Ho_Chi_Minh",
      kind: "command",
      if_revision: 3,
      command: "make test",
      repo: "lumen-api",
      timeout_minutes: 45,
    });
    const agent = { ...emptyForm("UTC"), name: "standup", agent: "@pm", prompt: " Post the standup " };
    expect(bodyOf(agent, 0)).toEqual({ cron: "0 9 * * 1-5", timezone: "UTC", kind: "agent", if_revision: 0, agent: "pm", chat_id: null, prompt: "Post the standup" });
  });

  it("marks the fields to fix before it writes", () => {
    expect(formProblems(emptyForm("UTC"), true)).toEqual(["name", "agent", "prompt"]);
    expect(formProblems({ ...formOf(job), cron: "0 2 * *", timeout: "300" }, false)).toEqual(["cron", "timeout"]);
    expect(formProblems({ ...emptyForm(""), name: "Bad Name", kind: "plan" }, true)).toEqual(["name", "timezone", "plan"]);
  });
});
