"use client";

import {
  ChartLine,
  CircleCheck,
  CirclePause,
  CircleX,
  Coins,
  Info,
  Lightbulb,
  type LucideIcon,
  RotateCw,
  Tags,
} from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { type ReactNode, type RefObject, useEffect, useId, useRef } from "react";

import { NAME_LINK } from "@/components/data/identifier";
import type { HubQueryState } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { markTodaySeen, type Today, type TodayItem } from "@/lib/notification-queries";
import { cn } from "@/lib/utils";

import { CARD_LINK, Relative } from "./parts";
import { type ItemKind, summaryOf, type TodayWindow, windowOf } from "./today-model";

const SETTINGS_HREF = "/settings/notifications" as Route;

/** Each kind of item, its icon and the tone of its mark (web/DESIGN.md, Iconography). */
const LOOK: Record<ItemKind, { icon: LucideIcon; tone: string }> = {
  release: { icon: Tags, tone: "text-success" },
  step_done: { icon: CircleCheck, tone: "text-success" },
  run_failed: { icon: CircleX, tone: "text-danger" },
  run_parked: { icon: CirclePause, tone: "text-attention" },
  problem: { icon: ChartLine, tone: "text-muted-foreground" },
  cost: { icon: Coins, tone: "text-muted-foreground" },
  proposal: { icon: Lightbulb, tone: "text-muted-foreground" },
  report: { icon: Info, tone: "text-muted-foreground" },
};

/**
 * Move the seen mark once the end of Today has shown, with the page in view: the line under the last item is on screen,
 * so every item above it was there to read. The mark goes to `seen_until`, the moment of the last item when the list
 * was cut, so nothing the page did not show is passed over. Once a reading; a failed write is tried again when the line
 * shows next.
 */
function useMarkSeen(today: Today, end: RefObject<HTMLElement | null>) {
  const posted = useRef<string | null>(null);
  useEffect(() => {
    const element = end.current;
    if (!element || posted.current === today.seen_until) return;
    let inView = typeof IntersectionObserver === "undefined";
    const post = () => {
      if (!inView || document.visibilityState !== "visible" || posted.current === today.seen_until) return;
      posted.current = today.seen_until;
      markTodaySeen(browserApi(), today.seen_until).catch(() => {
        posted.current = null;
      });
    };
    const observer =
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver((entries) => {
            inView = entries.some((entry) => entry.isIntersecting);
            post();
          });
    observer?.observe(element);
    document.addEventListener("visibilitychange", post);
    post();
    return () => {
      observer?.disconnect();
      document.removeEventListener("visibilitychange", post);
    };
  }, [today.seen_until, end]);
}

/** The summary in the visitor's language: the hub's own sentence in English, the same counts in other words else. */
function useSummary() {
  const t = useTranslations("home.today.summary");
  const locale = useLocale();
  return (today: Today) => {
    if (locale === "en") return today.summary;
    const list = new Intl.ListFormat(locale, { type: "conjunction" });
    return summaryOf(today, {
      asks: (count) => t("asks", { count }),
      nothing: (asks, window) => t("nothing", { asks, window }),
      part: (kind, count) => t(`part.${kind}`, { count }),
      changed: (asks, window, parts, more) => t("changed", { asks, window, parts, more: more ? "yes" : "no" }),
      list: (parts) => list.format(parts),
    });
  };
}

function ItemRow({ item }: { item: TodayItem }) {
  const t = useTranslations("home.today");
  const { icon: Icon, tone } = LOOK[item.kind];
  return (
    <li className="grid grid-cols-[20px_minmax(0,1fr)] gap-x-3 border-t px-4 py-3 transition-colors first:border-t-0 hover:bg-accent" data-testid="today-item" data-kind={item.kind}>
      <span className="flex justify-center pt-0.5">
        <Icon className={cn("size-4", tone)} aria-hidden="true" />
      </span>
      <div className="flex min-w-0 flex-col gap-0.5">
        <Link href={item.link as Route} className={cn(NAME_LINK, "text-sm leading-5 font-medium text-pretty [overflow-wrap:anywhere]")} data-testid="today-item-link">
          <span className="sr-only">{t(`kind.${item.kind}`)}:</span> {item.title}
        </Link>
        {item.body ? <p className="line-clamp-2 text-[13px] leading-[18px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{item.body}</p> : null}
        <p className="flex min-w-0 flex-wrap items-center gap-x-2 text-xs leading-4 text-fg-subtle">
          <span className="font-mono [overflow-wrap:anywhere]">{item.project}</span>
          <Relative value={item.at} />
        </p>
      </div>
    </li>
  );
}

function Frame({ window, children, testId }: { window: TodayWindow | null; children: ReactNode; testId: string }) {
  const t = useTranslations("home.today");
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className="min-w-0 rounded-md border bg-card shadow-raised" data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-2 gap-y-1 border-b px-4 py-2">
        <h2 id={headingId} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        {window ? (
          <span className="text-xs text-fg-subtle" data-testid="today-window">
            {t(`window.${window}`)}
          </span>
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          <Link href={SETTINGS_HREF} className={CARD_LINK} data-testid="today-settings">
            {t("settings")}
          </Link>
        </div>
      </div>
      {children}
    </section>
  );
}

function TodayView({ today }: { today: Today }) {
  const t = useTranslations("home.today");
  const summary = useSummary();
  const end = useRef<HTMLParagraphElement>(null);
  useMarkSeen(today, end);
  return (
    <Frame window={windowOf(today)} testId="today">
      <p className="px-4 py-3 text-[15px] leading-[22px] text-pretty text-foreground" data-testid="today-summary">
        {summary(today)}
      </p>
      {today.items.length > 0 ? (
        <ul className="border-t" aria-label={t("items")} data-testid="today-items">
          {today.items.map((item, index) => (
            <ItemRow key={`${item.kind}-${item.link}-${item.at}-${index}`} item={item} />
          ))}
        </ul>
      ) : null}
      <p
        ref={end}
        className="flex items-center gap-2 border-t px-4 py-2.5 text-[13px] leading-[18px] text-muted-foreground"
        data-testid={today.caught_up ? "today-caught-up" : "today-more"}
      >
        {today.caught_up ? <CircleCheck className="size-4 shrink-0 text-success" aria-hidden="true" /> : <Info className="size-4 shrink-0" aria-hidden="true" />}
        {today.caught_up ? t("caughtUp") : t("more")}
      </p>
    </Frame>
  );
}

/**
 * Today, Home's first card (docs/notifications.md, Today): one sentence of what waits for the visitor and what changed
 * since they last looked, the things that changed (at most eight, each a link), and whether that is everything. It is
 * read once a visit, so what it lists stays while the visitor reads; once its last line has shown, the hub moves the
 * seen mark past what was listed. A failed read stays inside the card, with Try again.
 */
export function TodayCard({ state }: { state: HubQueryState<Today> }) {
  const t = useTranslations("home.today");
  if (state.status === "success") return <TodayView today={state.data} />;
  if (state.status === "error") {
    return (
      <Frame window={null} testId="today">
        <div className="flex flex-wrap items-center gap-2 px-4 py-3 text-sm text-danger" role="alert" data-testid="today-error">
          {t("loadFailed")}
          <Button type="button" variant="outline" size="sm" onClick={state.retry}>
            <RotateCw aria-hidden="true" />
            {t("retry")}
          </Button>
        </div>
      </Frame>
    );
  }
  return (
    <Frame window={null} testId="today">
      <div className="flex flex-col gap-2 px-4 py-3" role="status" aria-label={t("loading")} data-testid="today-loading">
        <Skeleton className="h-4 w-3/4 rounded-xs" />
        <Skeleton className="h-4 w-1/2 rounded-xs" />
      </div>
    </Frame>
  );
}
