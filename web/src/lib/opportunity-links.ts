import type { Route } from "next";

import type { components } from "@/lib/api/schema";

/**
 * Where a project's opportunities are, and how their codes and statuses read (plan opportunity-intake): what the
 * pages that only name an opportunity need, apart from the reads and writes of `lib/opportunity-queries.ts`.
 */
export type OpportunityStatus = components["schemas"]["ProjectOpportunity"]["status"];

/** `opportunities.STATUSES`: an open opportunity, then the three ways one closes. */
export const OPPORTUNITY_STATUSES = ["open", "captured", "missed", "dropped"] as const satisfies readonly OpportunityStatus[];
/** `opportunities.REF`: an opportunity's code. */
export const OPPORTUNITY_REF = /^O-[1-9][0-9]{0,8}$/;

export function opportunitiesHref(project: string, code?: string): Route {
  const base = `/p/${encodeURIComponent(project)}/opportunities`;
  return (code ? `${base}/${encodeURIComponent(code)}` : base) as Route;
}
