import type { Messages } from "next-intl";

import { type DocLink, README, ROADMAP } from "./links";

/** A paragraph of product copy, backed by the public documentation in both languages. */
export type ClaimKey = keyof Messages["product"]["claims"];

const HOST: DocLink = { file: "docs/hub-deployment.md" };
const WORKER: DocLink = { file: "docs/workers.md", anchor: "the-daemon" };

/** Each rich-text link resolves to a public reference, never a private harness or a removed README heading. */
export const CLAIMS: Record<ClaimKey, Record<string, DocLink>> = {
  lede: { readme: README },
  graph: {
    context: { file: "docs/hub.md", anchor: "memories-skills-and-knowledge-graphs" },
    graph: { file: "docs/kg-setup.md" },
  },
  plugins: {
    plugins: { file: "docs/plugins.md" },
    market: { file: "docs/market.md" },
  },
  planRuns: { verify: { file: "docs/workers.md", anchor: "plan-runs" } },
  machines: {
    worker: WORKER,
    policy: { file: "docs/autonomy.md" },
    host: HOST,
  },
  access: { host: HOST },
  status: { roadmap: ROADMAP },
};
