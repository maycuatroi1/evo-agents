import type { Messages } from "next-intl";

import type { ClaimKey } from "./claims";

export type NeedKey = keyof Messages["product"]["needs"]["rows"];
export type Need = { key: NeedKey; claim: ClaimKey };

/** Reader benefits, with each description linked to the documentation that supports it. */
export const NEEDS: Need[] = [
  { key: "plans", claim: "planRuns" },
  { key: "context", claim: "graph" },
  { key: "control", claim: "decisions" },
  { key: "machines", claim: "machines" },
];
