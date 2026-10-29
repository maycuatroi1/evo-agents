import type { Messages } from "next-intl";

import { type DocLink, README, ROADMAP } from "./links";

/** A paragraph of product copy, backed by the public documentation in both languages. */
export type ClaimKey = keyof Messages["product"]["claims"];

const HOST: DocLink = { file: "docs/hub-deployment.md" };
const WORKER: DocLink = { file: "docs/workers.md", anchor: "the-daemon" };

/** Each rich-text link resolves to a public reference, never a private harness or a removed README heading. */
export const CLAIMS: Record<ClaimKey, Record<string, DocLink>> = {
  lede: { readme: README },
  planRuns: { verify: { file: "docs/workers.md", anchor: "plan-runs" } },
  graph: {
    context: { file: "docs/hub.md", anchor: "memories-skills-and-knowledge-graphs" },
    graph: { file: "docs/kg-setup.md" },
  },
  decisions: {
    policy: { file: "docs/autonomy.md" },
    inbox: { file: "docs/workers.md", anchor: "decisions-and-notices" },
  },
  machines: { worker: WORKER, host: HOST },
  access: { host: HOST },
  status: { roadmap: ROADMAP },
};
