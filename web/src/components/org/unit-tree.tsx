"use client";

import { Archive, ChevronRight, FolderPlus, OctagonX, SlidersHorizontal, Pencil, UserPlus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

import { useOrgView } from "./context";
import type { UnitNode } from "./model";
import { ActionMenu, LevelLine, type MenuItem, SeatRow } from "./seat-row";
import { StopFlag } from "./stop-parts";
import { heldStop, unitTarget } from "./stops";

function useUnitItems(node: UnitNode): MenuItem[] {
  const t = useTranslations("org.actions");
  const { rights, autonomy, mayStop } = useOrgView();
  const { unit } = node;
  if (unit.archived_at) return [];
  const items: MenuItem[] = [];
  if (rights.createUnder(unit)) items.push({ id: "create-unit", label: t("createUnit"), icon: FolderPlus, action: { kind: "createUnit", parent: unit } });
  if (rights.roles(unit.slug).length > 0) items.push({ id: "add-seat", label: t("addSeat"), icon: UserPlus, action: { kind: "addSeat", unit: unit.slug } });
  if (rights.editUnit(unit)) {
    items.push({ id: "edit-unit", label: t("editUnit"), icon: Pencil, action: { kind: "editUnit", unit } });
    items.push({ id: "unit-ceiling", label: t("setCeiling"), icon: SlidersHorizontal, action: { kind: "unitCeiling", unit } });
  }
  const target = unitTarget(unit);
  if (autonomy && !heldStop(autonomy, target) && mayStop(target)) items.push({ id: "stop-unit", label: t("stopUnit"), icon: OctagonX, action: { kind: "stop", target }, destructive: true });
  if (rights.editUnit(unit)) {
    items.push({ id: "archive-unit", label: t("archiveUnit"), icon: Archive, action: { kind: "archiveUnit", unit }, destructive: true });
  }
  return items;
}

/**
 * One unit of the chart and everything below it: a row that opens and closes the unit (Enter or Space on its name),
 * its seats, then the units below it, each indented one step.
 */
export function UnitBranch({ node }: { node: UnitNode }) {
  const t = useTranslations("org");
  const tKind = useTranslations("org.kinds");
  const [open, setOpen] = useState(true);
  const panel = useId();
  const items = useUnitItems(node);
  const { unit, seats, children } = node;
  const { autonomy } = useOrgView();
  const stop = unit.archived_at ? undefined : heldStop(autonomy, unitTarget(unit));
  const count = seats.length + children.length;

  return (
    <li className="min-w-0" data-testid="org-unit" data-slug={unit.slug} data-archived={unit.archived_at ? "true" : undefined}>
      <div className="flex min-w-0 items-start gap-2 px-2 py-2">
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          aria-expanded={open}
          aria-controls={panel}
          className="flex min-w-0 flex-1 items-start gap-2 rounded-sm text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50 max-md:min-h-11"
          data-testid="org-unit-toggle"
        >
          <ChevronRight className={cn("mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden="true" />
          <span className="flex min-w-0 flex-col gap-1">
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span className="text-sm font-semibold [overflow-wrap:anywhere]" data-testid="org-unit-name">
                {unit.name}
              </span>
              <Tag>{tKind(unit.kind)}</Tag>
              <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{unit.slug}</span>
              {unit.archived_at ? (
                <Badge variant="secondary" data-testid="org-unit-archived">
                  {t("archived")}
                </Badge>
              ) : null}
            </span>
            <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <LevelLine of={unit} />
              {unit.kind === "cohort" && unit.ends_on ? <span className="text-xs text-muted-foreground">{t("endsOn", { day: unit.ends_on })}</span> : null}
              {open ? null : <span className="text-xs text-muted-foreground">{t("hidden", { count })}</span>}
            </span>
          </span>
        </button>
        {stop ? <StopFlag stop={stop} testId="org-unit-stopped" /> : null}
        <ActionMenu label={t("actions.unitMenu", { name: unit.name })} items={items} testId="org-unit-menu" />
      </div>
      <div id={panel} hidden={!open} className="min-w-0 border-l border-border pl-2 ml-3 sm:ml-4 sm:pl-3">
        {seats.length > 0 ? (
          <ul className="flex min-w-0 flex-col divide-y divide-border rounded-md border bg-card" aria-label={t("seatsOf", { name: unit.name })}>
            {seats.map((seat) => (
              <SeatRow key={seat.id} seat={seat} />
            ))}
          </ul>
        ) : (
          <p className="px-2 py-1 text-xs text-muted-foreground">{t("noSeats")}</p>
        )}
        {children.length > 0 ? (
          <ul className="flex min-w-0 flex-col" aria-label={t("unitsBelow", { name: unit.name })}>
            {children.map((child) => (
              <UnitBranch key={child.unit.slug} node={child} />
            ))}
          </ul>
        ) : null}
      </div>
    </li>
  );
}
