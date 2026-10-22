"use client";

import { createContext, useContext } from "react";

import type { PersonRuns, Rights } from "./model";
import type { OrgSeat, OrgUnit } from "./queries";

/** What a row of the tree may ask the page to open: a dialog or a confirmation. */
export type OrgAction =
  | { kind: "createUnit"; parent: OrgUnit }
  | { kind: "editUnit"; unit: OrgUnit }
  | { kind: "archiveUnit"; unit: OrgUnit }
  | { kind: "unitCeiling"; unit: OrgUnit }
  | { kind: "addSeat"; unit: string; login?: string }
  | { kind: "editSeat"; seat: OrgSeat }
  | { kind: "endSeat"; seat: OrgSeat }
  | { kind: "seatCeiling"; seat: OrgSeat };

/** What every row of the tree reads: the chart's units, the viewer's rights, the runs at work, today and the actions. */
export type OrgView = {
  units: readonly OrgUnit[];
  rights: Rights;
  runs: Map<string, PersonRuns>;
  today: string | null;
  act: (action: OrgAction) => void;
};

export const OrgViewContext = createContext<OrgView | null>(null);

export function useOrgView(): OrgView {
  const view = useContext(OrgViewContext);
  if (!view) throw new Error("useOrgView outside OrgViewContext");
  return view;
}
