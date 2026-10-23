"use client";

import { Bot, Ellipsis, SlidersHorizontal, Hourglass, type LucideIcon, OctagonX, Pencil, Activity, Star, UserMinus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { Fragment } from "react";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { type OrgAction, useOrgView } from "./context";
import { endDay, runsHref, seatNote } from "./model";
import type { OrgSeat, OrgUnit } from "./queries";
import { SeatClasses, StopFlag } from "./stop-parts";
import { heldStop, seatTarget } from "./stops";

export type MenuItem = { id: string; label: string; icon: LucideIcon; action: OrgAction; destructive?: boolean };

/** A row's actions as a menu behind one icon button; nothing at all when the viewer may do none of them. */
export function ActionMenu({ label, items, testId }: { label: string; items: MenuItem[]; testId: string }) {
  const { act } = useOrgView();
  if (items.length === 0) return null;
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={label} data-testid={testId}>
          <Ellipsis aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-48">
        {items.map((item, index) => (
          <Fragment key={item.id}>
            {item.destructive && index > 0 ? <DropdownMenuSeparator /> : null}
            <DropdownMenuItem variant={item.destructive ? "destructive" : "default"} onSelect={() => act(item.action)} data-testid={`org-action-${item.id}`}>
              <item.icon aria-hidden="true" />
              {item.label}
            </DropdownMenuItem>
          </Fragment>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Where a level comes from, in words: a unit by name, this seat, or another seat. */
export function useSourceText() {
  const t = useTranslations("org");
  const { units } = useOrgView();
  return (source: string, seat?: OrgSeat) => {
    const [kind, id] = source.split(":");
    if (kind === "seat") return Number(id) === seat?.id ? t("fromThisSeat") : t("fromSeat", { id: id ?? "" });
    return t("fromUnit", { name: units.find((unit) => unit.slug === id)?.name ?? id ?? "" });
  };
}

/** The level a unit or seat works at, from where, and its own ceiling when it sets one. */
export function LevelLine({ of, seat }: { of: OrgUnit | OrgSeat; seat?: OrgSeat }) {
  const t = useTranslations("org");
  const sourceText = useSourceText();
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-1 text-xs text-muted-foreground" data-testid="org-level-line">
      <span className="font-medium text-foreground" data-testid="org-effective">
        {t("effective", { level: of.effective })}
      </span>
      <span>{sourceText(of.source, seat)}</span>
      <span aria-hidden="true">·</span>
      <span data-testid="org-ceiling">{of.ceiling ? t("ceiling", { level: of.ceiling }) : t("noCeiling")}</span>
    </span>
  );
}

function RunsLink({ login }: { login: string }) {
  const t = useTranslations("org");
  const { runs } = useOrgView();
  const found = runs.get(login);
  if (!found || found.total === 0) return null;
  if (found.projects.length === 1) {
    const [only] = found.projects;
    return (
      <Link href={runsHref(only.project, login)} className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline" data-testid="org-seat-runs">
        <Activity className="size-3" aria-hidden="true" />
        {t("running", { count: found.total, project: only.project })}
      </Link>
    );
  }
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="link" size="sm" className="h-auto px-0 text-xs" data-testid="org-seat-runs">
          <Activity className="size-3" aria-hidden="true" />
          {t("runningMany", { count: found.total, projects: found.projects.length })}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-48">
        <DropdownMenuLabel>{t("runsOf", { login })}</DropdownMenuLabel>
        {found.projects.map(({ project, running }) => (
          <DropdownMenuItem key={project} asChild>
            <Link href={runsHref(project, login)}>{t("runningIn", { count: running, project })}</Link>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** One seat: who holds it and how, the level it works at, its runs at work, and its end when it is near or past. */
export function SeatRow({ seat }: { seat: OrgSeat }) {
  const t = useTranslations("org");
  const tRole = useTranslations("org.roles");
  const { units, rights, today, autonomy, mayStop } = useOrgView();
  const note = today ? seatNote(units, seat, today) : null;
  const ends = endDay(units, seat);
  const who = seat.login ?? seat.agent ?? "";
  const items: MenuItem[] = [];
  const live = !seat.ended_at;
  if (live && rights.seat(seat.unit, seat.role)) items.push({ id: "edit-seat", label: t("actions.editSeat"), icon: Pencil, action: { kind: "editSeat", seat } });
  if (live && rights.seatCeiling(seat)) items.push({ id: "seat-ceiling", label: t("actions.setCeiling"), icon: SlidersHorizontal, action: { kind: "seatCeiling", seat } });
  const target = live ? seatTarget(seat) : null;
  const stop = target ? heldStop(autonomy, target) : undefined;
  if (target && autonomy && !stop && mayStop(target)) items.push({ id: "stop-runs", label: t("actions.stopRuns"), icon: OctagonX, action: { kind: "stop", target }, destructive: true });
  if (live && rights.seat(seat.unit, seat.role)) items.push({ id: "end-seat", label: t("actions.endSeat"), icon: UserMinus, action: { kind: "endSeat", seat }, destructive: true });
  const details = [
    seat.reports_to ? t("reportsTo", { login: seat.reports_to }) : null,
    seat.sponsor ? t("sponsor", { login: seat.sponsor }) : null,
    ends ? t("endsOn", { day: ends }) : null,
  ].filter((detail): detail is string => detail !== null);

  return (
    <li className="flex min-w-0 items-start gap-3 px-3 py-2.5" data-testid="org-seat" data-seat-id={seat.id} data-login={seat.login ?? undefined} data-note={note?.kind}>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          {seat.agent ? <Bot className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" /> : null}
          <span className="min-w-0 font-mono text-[13px] font-medium [overflow-wrap:anywhere]" data-testid="org-seat-who">
            {who}
          </span>
          {seat.project ? <span className="font-mono text-xs text-muted-foreground">{seat.project}</span> : null}
          <Tag data-testid="org-seat-role">{tRole(seat.role)}</Tag>
          {seat.primary ? (
            <Tag data-testid="org-seat-primary">
              <Star aria-hidden="true" />
              {t("primary")}
            </Tag>
          ) : null}
          {note?.kind === "ended" ? (
            <Badge variant="secondary" data-testid="org-seat-ended">
              {t("ended")}
            </Badge>
          ) : null}
          {note?.kind === "endsSoon" ? (
            <Badge variant="warning" data-testid="org-seat-ends-soon">
              <Hourglass aria-hidden="true" />
              {note.days === 0 ? t("endsToday") : t("endsIn", { count: note.days })}
            </Badge>
          ) : null}
          {stop ? <StopFlag stop={stop} testId="org-seat-stopped" /> : null}
        </div>
        {details.length > 0 ? <p className="text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]">{details.join(" · ")}</p> : null}
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <LevelLine of={seat} seat={seat} />
          {seat.login ? <RunsLink login={seat.login} /> : null}
        </div>
        {live ? <SeatClasses seat={seat} /> : null}
      </div>
      <ActionMenu label={t("actions.seatMenu", { who })} items={items} testId="org-seat-menu" />
    </li>
  );
}
