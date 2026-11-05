import type { Run } from "./queries";

/**
 * What a run spent as the hub's ledger keeps it (`Run.spend`, evo_agents/hub/run_spend.py): its own tokens and cost,
 * by model, from its usage reports as they came, so a run that runs, waits or is parked shows them too, and a run that
 * went on in a session counts only its share. A cost is the runtime's own (`reported_usd`), the tokens no reported
 * cost covers at the model's list price (`estimated_usd`), or both; a model without a list price has no estimate.
 */

export type Spend = NonNullable<Run["spend"]>;
export type ModelSpend = Spend["models"][number];

/** How a cost came: all reported, all estimated, or some of each; null when there is none. */
export type CostSource = "reported" | "estimated" | "mixed";

export function costSource(spend: { reported_usd: number | null; estimated_usd: number | null }): CostSource | null {
  const reported = spend.reported_usd !== null;
  const estimated = spend.estimated_usd !== null && spend.estimated_usd > 0;
  if (reported && estimated) return "mixed";
  if (reported) return "reported";
  return spend.estimated_usd !== null ? "estimated" : null;
}

/** The run's cost the list shows: the ledger's, else the usage its worker reported with its end (a hub before 0210). */
export function runCost(run: Pick<Run, "spend">): { amount: number; source: CostSource } | null {
  const spend = run.spend;
  if (!spend || spend.cost_usd === null) return null;
  return { amount: spend.cost_usd, source: costSource(spend) ?? "reported" };
}

/** A model's name as the card shows it: the runtime's id, or a dash for a report that named none. */
export function modelLabel(model: string): string {
  return model || "-";
}
