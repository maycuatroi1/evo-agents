import { describe, expect, it } from "vitest";

import { isMoney, ledgerTone, outcomeFigures, releaseOf, replayOf, revertProposal, safeUrl, shortSha, triggerFigures } from "./ledger-model";

describe("a line of the ledger", () => {
  it("marks merges, passed Judges and kept outcomes in success, failures and reverts in danger, an open change in attention", () => {
    expect(ledgerTone({ action: "merged", outcome: null, verdict: null })).toBe("success");
    expect(ledgerTone({ action: "promoted", outcome: null, verdict: null })).toBe("success");
    expect(ledgerTone({ action: "judged", outcome: null, verdict: { passed: true } })).toBe("success");
    expect(ledgerTone({ action: "judged", outcome: null, verdict: { passed: false } })).toBe("danger");
    expect(ledgerTone({ action: "build_failed", outcome: null, verdict: null })).toBe("danger");
    expect(ledgerTone({ action: "outcome", outcome: "keep", verdict: null })).toBe("success");
    expect(ledgerTone({ action: "outcome", outcome: "revert", verdict: null })).toBe("danger");
    expect(ledgerTone({ action: "outcome", outcome: "unclear", verdict: null })).toBe("neutral");
    expect(ledgerTone({ action: "left_open", outcome: null, verdict: null })).toBe("attention");
    expect(ledgerTone({ action: "proposed", outcome: null, verdict: null })).toBe("neutral");
  });

  it("writes a commit as its first 12 characters and opens only https pull requests", () => {
    expect(shortSha("0123456789abcdef0123456789abcdef01234567")).toBe("0123456789ab");
    expect(safeUrl("https://github.com/o/r/pull/1")).toBe("https://github.com/o/r/pull/1");
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl(null)).toBeNull();
  });

  it("reads the figures that set a proposal off, keeping only whole entries", () => {
    const figures = {
      activity: 12,
      metrics: [
        { key: "environment", what: "failures from the environment", value: 5 },
        { key: "cost_usd", value: 1.5 },
        { key: "broken" },
        "not an entry",
      ],
    };
    expect(triggerFigures(figures)).toEqual({
      activity: 12,
      figures: [
        { key: "environment", what: "failures from the environment", value: 5 },
        { key: "cost_usd", what: "cost_usd", value: 1.5 },
      ],
    });
    expect(triggerFigures(null)).toEqual({ activity: null, figures: [] });
    expect(isMoney("cost_usd") && !isMoney("environment")).toBe(true);
  });

  it("reads an outcome's figures before and after the merge, and the revert it proposed", () => {
    const figures = {
      activity_before: 3,
      activity_after: 4,
      reason: "the figures got worse: failures from the environment",
      metrics: [
        { key: "environment", what: "failures from the environment", before: 3, after: 12, before_rate: 1, after_rate: 3, change: "worse" },
        { key: "corrections", before: 1, after: 0, change: "sideways" },
      ],
    };
    expect(outcomeFigures(figures)).toEqual({
      before: 3,
      after: 4,
      reason: "the figures got worse: failures from the environment",
      figures: [
        { key: "environment", what: "failures from the environment", value: 3, after: 12, beforeRate: 1, afterRate: 3, change: "worse" },
      ],
    });
    expect(revertProposal({ details: { revert_proposal_id: 9, outcome_days: 7 } })).toBe(9);
    expect(revertProposal({ details: { outcome_days: 7 } })).toBeNull();
    expect(revertProposal({ details: null })).toBeNull();
  });

  it("reads a skill release, its mark in success only when the hub published something", () => {
    const published = { kind: "publish", skill: "deploy", scope: "global", version: 4, previous: 3, published: true, sha256: "f" };
    expect(releaseOf(published)).toEqual({ kind: "publish", skill: "deploy", published: true, version: 4, previous: 3, reverted: null, why: null });
    expect(releaseOf({ kind: "republish", skill: "deploy", reverted: 4, previous: 3, version: 5, published: true })).toMatchObject({ kind: "republish", reverted: 4, version: 5 });
    expect(releaseOf({ kind: "publish", skill: "deploy", published: false, why: "its repo has no origin on GitHub" })).toMatchObject({ published: false, why: "its repo has no origin on GitHub" });
    expect(releaseOf({ kind: "other", skill: "deploy" })).toBeNull();
    expect(releaseOf(null)).toBeNull();
    expect(ledgerTone({ action: "skill_release", outcome: null, verdict: null, details: published })).toBe("success");
    expect(ledgerTone({ action: "skill_release", outcome: null, verdict: null, details: { ...published, published: false } })).toBe("neutral");
  });

  it("finds the replays a merge rests on and the ones an outcome kept", () => {
    const replay = { won: true, tasks: 6 };
    expect(replayOf({ action: "merged", details: { replay }, figures: null })).toBe(replay);
    expect(replayOf({ action: "outcome", details: null, figures: { replay } })).toBe(replay);
    expect(replayOf({ action: "judged", details: { replay }, figures: null })).toBeNull();
    expect(replayOf({ action: "merged", details: { release: {} }, figures: null })).toBeNull();
  });
});
