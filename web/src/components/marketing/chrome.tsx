"use client";

import { useQuery } from "@tanstack/react-query";
import { CalendarRange, Eye, LayoutDashboard, ListChecks } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import { Meter, METER_TEXT, type MeterTone } from "@/components/data/meter";
import { TabNav } from "@/components/data/tab-nav";
import { Banner } from "@/components/feedback/banner";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { budgetUsed, MARKETING_OVERVIEW, MARKETING_POSTS, MARKETING_WEEKLY, type MarketingBudget, type MarketingPage, marketingHref } from "./api";

export type MarketingRole = "reader" | "writer" | "admin";

/** The visitor's role on the project, null until whoami answers (or for someone with no grant on it). */
export function useMarketingRole(project: string): MarketingRole | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  const role = data?.grants.find((grant) => grant.project === project)?.role ?? null;
  return role === "reader" || role === "writer" || role === "admin" ? role : null;
}

export function canWrite(role: MarketingRole | null): boolean {
  return role === "writer" || role === "admin";
}

/** A reader's note: they read the channels, the budget and the week; nothing on the page writes for them. */
export function ReaderNote({ project, text }: { project: string; text?: string }) {
  const t = useTranslations("marketing");
  return (
    <Banner icon={Eye} title={t("readerTitle", { project })} testId="marketing-read-only">
      {text ?? t("readerText")}
    </Banner>
  );
}

/** The plugin's three pages as tabs under the page head. */
export function MarketingTabs({ project, current }: { project: string; current: MarketingPage }) {
  const t = useTranslations("marketing.tabs");
  return (
    <TabNav
      label={t("label")}
      current={current}
      tabTestId="marketing-tab"
      tabs={[
        { id: MARKETING_OVERVIEW, label: t("overview"), href: marketingHref(project, MARKETING_OVERVIEW), icon: LayoutDashboard },
        { id: MARKETING_POSTS, label: t("posts"), href: marketingHref(project, MARKETING_POSTS), icon: ListChecks },
        { id: MARKETING_WEEKLY, label: t("weekly"), href: marketingHref(project, MARKETING_WEEKLY), icon: CalendarRange },
      ]}
    />
  );
}

/** Dollars as the hub counts them, to the cent. */
export function useMoney() {
  const format = useFormatter();
  return (usd: number) => format.number(usd, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function toneOf(used: number): MeterTone {
  if (used >= 100) return "danger";
  if (used >= 80) return "warning";
  return "normal";
}

/** The month's cost against its cap: a meter, and the figures written beside it so the colour is never the only cue. */
export function BudgetMeter({ budget, className }: { budget: MarketingBudget; className?: string }) {
  const t = useTranslations("marketing.budget");
  const money = useMoney();
  const id = useId();
  const used = budgetUsed(budget);
  const tone = toneOf(used);
  const figures = t("figures", { used: money(budget.spent_usd + budget.reserved_usd), cap: money(budget.budget_usd) });
  return (
    <div className={cn("flex min-w-0 flex-col gap-2", className)} data-testid="marketing-budget">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span id={id} className="text-[13px] text-muted-foreground">
          {t("label", { month: budget.month })}
        </span>
        <span className={cn("text-[15px] font-semibold tabular-nums", METER_TEXT[tone])} data-testid="marketing-budget-figures">
          {figures}
        </span>
      </div>
      <Meter value={used} tone={tone} labelledBy={id} valueText={figures} />
      <p className="text-xs text-muted-foreground tabular-nums">
        {t("split", { spent: money(budget.spent_usd), reserved: money(budget.reserved_usd) })}
        {used >= 100 ? <span className="font-medium text-danger"> {t("spentAll")}</span> : null}
      </p>
    </div>
  );
}
