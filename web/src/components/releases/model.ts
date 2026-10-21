import type { MeterTone } from "@/components/data/meter";

import type { CheckOutcome, ReleaseCheck, ReleaseCycle, ReleaseStatus, ReleaseTag } from "./queries";

/**
 * The Releases page's pure parts: what the visitor may do, where a tag, a commit and a workflow lead on GitHub, how
 * much of a cycle's cap its runs spent, what went wrong that the page must say, and how a cycle that ended ended.
 */

/** The problem of a CI the hub could not read because the App's installation grants no actions (release_ci). */
export const ACTIONS_PERMISSION = "actions_permission";
const GITHUB = "https://github.com";

/** An admin of the project pauses, resumes and runs a check now; everyone else with a grant reads. */
export function releaseRights(viewer: { role: string | null } | null): { admin: boolean } {
  return { admin: viewer?.role === "admin" };
}

/** The first 7 characters of a commit, as GitHub shows one. */
export function shortCommit(commit: string | null | undefined): string {
  return (commit ?? "").slice(0, 7);
}

/** The repo on GitHub, a tag of it or a commit of it; null when the schedule's repo is not on GitHub any more. */
export function githubRepoUrl(repo: string | null | undefined): string | null {
  return repo ? `${GITHUB}/${repo}` : null;
}

export function githubTagUrl(repo: string | null | undefined, tag: string): string | null {
  return repo ? `${GITHUB}/${repo}/tree/${encodeURIComponent(tag)}` : null;
}

export function githubCommitUrl(repo: string | null | undefined, commit: string | null | undefined): string | null {
  return repo && commit ? `${GITHUB}/${repo}/commit/${commit}` : null;
}

/** An address the page may link to: http or https only, so a value the hub stored never runs as script. */
export function safeUrl(url: string | null | undefined): string | null {
  return url && /^https?:\/\//i.test(url) ? url : null;
}

/** The share of the cap the runs spent, 0 to 1 (more when they went over it). */
export function budgetShare(cost: number, budget: number): number {
  return budget > 0 ? Math.max(cost, 0) / budget : 0;
}

/** The meter's tone for that share: the cap nearly spent asks for attention, spent is danger. */
export function budgetTone(share: number): MeterTone {
  if (share >= 0.9) return "danger";
  if (share >= 0.75) return "warning";
  return "normal";
}

/** Whether a tag's CI, or the newest check, could not be read because the App's installation grants no actions. */
export function lacksActions(status: Pick<ReleaseStatus, "cycle" | "last_check">): boolean {
  const tags = status.cycle?.tags ?? [];
  return tags.some((tag) => (tag.problem ?? "").startsWith(ACTIONS_PERMISSION)) || (status.last_check?.reason ?? "").includes(ACTIONS_PERMISSION);
}

/** The tone of a check's outcome: a person is needed for a pause or an owner without the admin role, and an error failed. */
export function outcomeTone(outcome: CheckOutcome): "neutral" | "attention" | "danger" {
  if (outcome === "error") return "danger";
  if (outcome === "paused" || outcome === "owner") return "attention";
  return "neutral";
}

/** The tag a cycle published: the newest it followed whose CI passed, or null. */
export function publishedTag(cycle: Pick<ReleaseCycle, "state" | "tags">): ReleaseTag | null {
  if (cycle.state !== "published") return null;
  return [...cycle.tags].reverse().find((tag) => tag.ci_state === "passed") ?? null;
}

/** The tag the hub follows now: the newest of the cycle's, the one whose CI it reads. */
export function watchedTag(cycle: Pick<ReleaseCycle, "tags">): ReleaseTag | null {
  return cycle.tags.at(-1) ?? null;
}

/** The test ids a tag failed, at most `limit`, with how many more there are. */
export function failedShown(tag: Pick<ReleaseTag, "failed_tests">, limit = 5): { shown: string[]; more: number } {
  return { shown: tag.failed_tests.slice(0, limit), more: Math.max(tag.failed_tests.length - limit, 0) };
}

/** Whether a tag has something to say under its row: tests that failed, a fault of the infrastructure, a problem. */
export function hasTagDetail(tag: Pick<ReleaseTag, "failed_tests" | "problem" | "infra">): boolean {
  return tag.failed_tests.length > 0 || tag.problem !== null || tag.infra;
}

/** Whether a check names a cycle it opened, so the page can link to it. */
export function openedCycle(check: Pick<ReleaseCheck, "outcome" | "cycle_id">): number | null {
  return check.outcome === "opened" ? check.cycle_id : null;
}
