import type { Messages } from "next-intl";

import type { ClaimKey } from "./claims";
import type { ProductImage } from "./images";

/** A row of the product page: `product.needs.rows.<key>` in messages/en.json and vi.json. */
export type NeedKey = keyof Messages["product"]["needs"]["rows"];

/** A public place where people asked for what a row answers, with its counts on the day they were read. */
export type Source =
  | {
      kind: "issue";
      url: string;
      /** owner/name of the repository on GitHub */
      repo: string;
      /** The issue's title as its author wrote it. */
      title: string;
      reactions: number;
      /** Open, or closed as completed on the given day: the page says so, so it never claims a tool still lacks it. */
      closedOn: string | null;
    }
  | { kind: "hn"; url: string; title: string; points: number; comments: number }
  | { kind: "article"; url: string; title: string; publisher: string };

export type Need = {
  key: NeedKey;
  /** Day the counts of `sources` were read, ISO 8601. */
  checkedOn: string;
  sources: Source[];
  /** What evo-agents does about it, each claim linked to its docs. */
  claims: ClaimKey[];
  image: ProductImage;
};

const CHECKED_ON = "2026-10-23";

function issue(repo: string, number: number, title: string, reactions: number, closedOn: string | null = null): Source {
  return { kind: "issue", url: `https://github.com/${repo}/issues/${number}`, repo, title, reactions, closedOn };
}

/**
 * The needs the page answers, from public issues and threads, with their counts on CHECKED_ON (reactions on GitHub's
 * API, points and comments on Hacker News's). needs.test.ts checks that each has at least one public source.
 */
export const NEEDS: Need[] = [
  {
    key: "unattended",
    checkedOn: CHECKED_ON,
    sources: [
      issue("anthropics/claude-code", 13354, "[FEATURE] Continue when the session limit reached", 215, "2026-10-22"),
      {
        kind: "hn",
        url: "https://news.ycombinator.com/item?id=48701512",
        title: "Show HN: Adrafinil – keep a lid-closed Mac awake only while agents work",
        points: 124,
        comments: 78,
      },
    ],
    claims: ["planRuns", "sleep"],
    image: "monitor",
  },
  {
    key: "away",
    checkedOn: CHECKED_ON,
    sources: [
      issue(
        "anthropics/claude-code",
        73125,
        "[BUG] AskUserQuestion: \"No response after 60s — continued without an answer\"",
        414,
        "2026-07-04",
      ),
      issue(
        "anthropics/claude-code",
        29438,
        "[FEATURE] Remote Control: Push notifications on iOS when permission approval is needed",
        69,
        "2026-08-17",
      ),
    ],
    claims: ["decisions", "waiting"],
    image: "plan-run",
  },
  {
    key: "context",
    checkedOn: CHECKED_ON,
    sources: [
      issue("anthropics/claude-code", 17428, "[Feature Request] Enhanced /compact with file-backed summaries and selective restoration", 117),
      issue(
        "anthropics/claude-code",
        27242,
        "[BUG] No working mechanism to review previous context after compaction, plan-mode clear, or branch navigation — data preserved but UI inaccessible",
        91,
      ),
      issue("openai/codex", 4106, "Control over auto-compaction parameters", 137),
      {
        kind: "article",
        url: "https://www.anthropic.com/research/effective-harnesses-for-long-running-agents",
        title: "Effective harnesses for long-running agents",
        publisher: "Anthropic",
      },
    ],
    claims: ["spool", "sessionPerStep"],
    image: "run-spool",
  },
  {
    key: "repos",
    checkedOn: CHECKED_ON,
    sources: [
      issue("anthropics/claude-code", 23627, "[FEATURE] Multi-repository support for remote/web sessions", 99),
      issue("openai/codex", 2909, "Support for multi-root workspaces", 153, "2026-08-06"),
      issue("openai/codex", 26338, "Codex App: support parent workspaces containing multiple Git repositories", 49),
    ],
    claims: ["graph", "worktrees"],
    image: "kg-search",
  },
];
