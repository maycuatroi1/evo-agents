import { ancestors, isEnded, rootOf } from "./model";
import type { HeldStop, OrgAutonomy, OrgChart, OrgSeat, OrgUnit, StopSet } from "./queries";

/**
 * Stops of autonomy on the Org page (plan autonomy-levels): who may stop or lift whose runs, as `org.stop_refusal`
 * decides (a person their own, a lead those of the units they lead and below, a person by their primary seat or the
 * root's without one, a hub admin anyone and the whole hub), and which stop holds a seat or a unit. The page hides what
 * the hub would refuse; the hub still decides. Pure functions, shared by the page and its tests.
 */

/** Whether unit `slug` is `top` or lies below it. */
function within(units: readonly OrgUnit[], slug: string, top: string): boolean {
  return ancestors(units, slug).some((unit) => unit.slug === top);
}

/** The unit of `login`'s primary live seat, the root's without one, as the hub reads it. */
export function primaryUnit(chart: Pick<OrgChart, "units" | "seats">, login: string, today: string): string | undefined {
  const seat = chart.seats.find((held) => held.login === login && held.primary && !isEnded(chart.units, held, today));
  return seat?.unit ?? rootOf(chart.units)?.slug;
}

export type StopRight = (target: StopSet) => boolean;

export function stopRightOf(chart: Pick<OrgChart, "units" | "seats" | "viewer">, today: string): StopRight {
  const { viewer, units } = chart;
  const led = viewer.leads;
  const underLead = (slug: string | undefined) => slug !== undefined && led.some((top) => within(units, slug, top));
  return (target) => {
    if (viewer.admin) return true;
    if (target.scope === "login") return target.login === viewer.login || (target.login ? underLead(primaryUnit(chart, target.login, today)) : false);
    if (target.scope === "unit") return underLead(target.unit ?? undefined);
    return false;
  };
}

/** The stop of exactly `target`'s scope that holds, if one does. */
export function heldStop(autonomy: OrgAutonomy | undefined, target: StopSet): HeldStop | undefined {
  return autonomy?.stops.find(
    (stop) =>
      stop.scope === target.scope &&
      (target.scope === "all" || (target.scope === "login" ? stop.login === target.login : stop.unit === target.unit)),
  );
}

export function seatTarget(seat: OrgSeat): StopSet | null {
  return seat.login ? { scope: "login", login: seat.login } : null;
}

export function unitTarget(unit: OrgUnit): StopSet {
  return { scope: "unit", unit: unit.slug };
}

export const ALL: StopSet = { scope: "all" };

/** What a stop is for, in the page's words: the person's login, the unit's name, or null for the whole hub. */
export function targetName(units: readonly OrgUnit[], target: StopSet): string | null {
  if (target.scope === "login") return target.login ?? "";
  if (target.scope === "unit") return units.find((unit) => unit.slug === target.unit)?.name ?? target.unit ?? "";
  return null;
}
