import type { Notification } from "./queries";

/** What a notice's `details` say, read as values of the right shape only (re-exported by `model.ts`). */

const COMMIT = /^[0-9a-f]{7,64}$/i;

export type NoticeFacts = { repo: string | null; branch: string | null; commits: string[] };

/** A notice's repo, branch and commits from its `details`, keeping only values of the right shape. */
export function noticeFacts(details: Notification["details"]): NoticeFacts {
  const text = (value: unknown) => (typeof value === "string" && value.trim() !== "" ? value : null);
  const commits = Array.isArray(details?.commits)
    ? details.commits.filter((commit): commit is string => typeof commit === "string" && COMMIT.test(commit))
    : [];
  return { repo: text(details?.repo), branch: text(details?.branch), commits };
}

export type PluginFacts = { plugin: string | null; topic: string | null };

/** The plugin that sent a notice and its topic, from `details`, keeping only text. */
export function pluginFacts(details: Notification["details"]): PluginFacts {
  const text = (value: unknown) => (typeof value === "string" && value.trim() !== "" ? value : null);
  return { plugin: text(details?.plugin), topic: text(details?.topic) };
}
