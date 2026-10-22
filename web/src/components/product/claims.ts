import type { Messages } from "next-intl";

import { type DocLink, README, ROADMAP } from "./links";

/** A sentence group of `product.claims` in messages/en.json and vi.json. */
export type ClaimKey = keyof Messages["product"]["claims"];

const WORKERS_DOC = "docs/workers.md";
const PLAN_RUNS: DocLink = { file: WORKERS_DOC, anchor: "plan-runs" };
const DECISIONS_AND_NOTICES: DocLink = { file: WORKERS_DOC, anchor: "decisions-and-notices" };
const SLEEP: DocLink = { file: WORKERS_DOC, anchor: "sleep-and-wake" };
const DECISIONS: DocLink = { file: "docs/notifications.md", anchor: "decisions" };
const SPOOL: DocLink = { file: "docs/hub.md", anchor: "memories-skills-and-knowledge-graphs" };
const TEAM_HUB: DocLink = { file: "README.md", anchor: "the-team-hub" };
const README_WORKERS: DocLink = { file: "README.md", anchor: "workers" };
const QUICK_START: DocLink = { file: "README.md", anchor: "quick-start" };

/**
 * The public docs behind each sentence the product page says about evo-agents: every claim's tags, each a link to the
 * section that describes the behaviour. claims.test.ts checks that each sentence of a claim, in both languages, holds
 * one of its tags, that each tag has a link here, and that each file and heading exists. Numbers come from these docs
 * only, never from a private harness.
 */
export const CLAIMS: Record<ClaimKey, Record<string, DocLink>> = {
  lede: { readme: README, workers: README_WORKERS },
  planRuns: { planRuns: PLAN_RUNS, verify: PLAN_RUNS, timeout: PLAN_RUNS },
  sleep: { sleep: SLEEP, retry: SLEEP },
  decisions: { decisions: DECISIONS, why: DECISIONS },
  waiting: {
    waits: DECISIONS_AND_NOTICES,
    inbox: DECISIONS_AND_NOTICES,
    telegram: { file: "docs/notifications.md", anchor: "what-a-decision-looks-like-there" },
    parks: DECISIONS_AND_NOTICES,
  },
  spool: { spool: SPOOL, head: SPOOL },
  sessionPerStep: { sessionPerStep: { file: WORKERS_DOC, anchor: "a-plan-run-with-a-session-for-each-step" } },
  graph: {
    harnessInit: QUICK_START,
    kgSearch: { file: "README.md", anchor: "using-it-from-claude-code" },
    kgImpact: { file: "docs/kg.md", anchor: "what-kg_impact-follows" },
  },
  worktrees: { planRuns: PLAN_RUNS },
  install: { graphify: { file: "README.md", anchor: "install" }, worker: README_WORKERS },
  login: { hubLogin: TEAM_HUB },
  plugins: {
    hubPlugin: TEAM_HUB,
    kgPlugin: { file: "README.md", anchor: "using-it-from-claude-code" },
    codex: TEAM_HUB,
  },
  next: { quickStart: QUICK_START, workers: README_WORKERS },
  roadmap: { roadmap: ROADMAP },
  loop: { loop: { file: "docs/loop.md" }, roadmap: ROADMAP },
  canvas: { canvas: { file: "docs/canvas-plugin.md" }, roadmap: ROADMAP },
  growth: { growth: ROADMAP },
  status: {
    pypi: { file: "README.md", anchor: "install" },
    license: { file: "README.md", anchor: "license" },
    invites: { file: "docs/hub.md", anchor: "invite-links" },
  },
};
