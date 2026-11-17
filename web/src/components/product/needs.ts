import type { Messages } from "next-intl";

import type { ClaimKey } from "./claims";

export type NeedKey = keyof Messages["product"]["needs"]["rows"];
export type Need = { key: NeedKey; claim: ClaimKey };

/** Reader benefits, with each description linked to the documentation that supports it. */
export const NEEDS: Need[] = [
  { key: "context", claim: "graph" },
  { key: "plugins", claim: "plugins" },
  { key: "plans", claim: "planRuns" },
  { key: "machines", claim: "machines" },
];
