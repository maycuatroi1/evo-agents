"use client";

import { createContext, useContext } from "react";

import type { PersonRuns, Rights } from "./model";
import type { OrgAutonomy, OrgSeat, OrgUnit, StopSet } from "./queries";
import type { StopRight } from "./stops";

/** What a row of the tree may ask the page to open: a dialog or a confirmation. */
export type OrgAction =
  | { kind: "createUnit"; parent: OrgUnit }
  | { kind: "editUnit"; unit: OrgUnit }
  | { kind: "archiveUnit"; unit: OrgUnit }
  | { kind: "unitCeiling"; unit: OrgUnit }
  | { kind: "addSeat"; unit: string; login?: string }
  | { kind: "editSeat"; seat: OrgSeat }
  | { kind: "endSeat"; seat: OrgSeat }
  | { kind: "seatCeiling"; seat: OrgSeat }
  | { kind: "stop"; target: StopSet };

/** What every row of the tree reads: the chart's units, the viewer's rights, the runs at work, today, the actions and autonomy. */
export type OrgView = {
  units: readonly OrgUnit[];
  rights: Rights;
  runs: Map<string, PersonRuns>;
  today: string | null;
  act: (action: OrgAction) => void;
  /** Every seat's effective classes and the stops that hold; undefined while it loads or when the read failed. */
  autonomy: OrgAutonomy | undefined;
  /** Whether the viewer may stop, or lift the stop of, a target. */
  mayStop: StopRight;
};

export const OrgViewContext = createContext<OrgView | null>(null);

export function useOrgView(): OrgView {
  const view = useContext(OrgViewContext);
  if (!view) throw new Error("useOrgView outside OrgViewContext");
  return view;
}
