"use client";

import { FileDiff, ListChecks, Radio } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

import { RUN_VIEW_PARAM } from "./queries";
import type { SessionTab } from "./run-log";

/** The run page's views: Live (the session, the agents, the step so far) and Progress (the run step by step). */
export type RunView = "live" | "progress";

export const PROGRESS_VIEW = "progress";

/**
 * The view the page shows, kept in the URL (`?view=progress`; Live has none, or the session tab it shows: `?view=log`).
 * Changing it replaces the URL without a navigation. `restore` is what the URL says again when the page goes back to
 * Live: the session tab the page still holds, null for the first.
 */
export function useRunView(offered: boolean, showSession: (tab: SessionTab) => void) {
  const params = useSearchParams();
  const [view, setView] = useState<RunView>(() => (offered && params.get(RUN_VIEW_PARAM) === PROGRESS_VIEW ? "progress" : "live"));
  const choose = useCallback((next: RunView, restore: string | null = null) => {
    setView(next);
    const url = new URL(window.location.href);
    url.searchParams.delete("tab");
    if (next === "progress") url.searchParams.set(RUN_VIEW_PARAM, PROGRESS_VIEW);
    else if (restore) url.searchParams.set(RUN_VIEW_PARAM, restore);
    else url.searchParams.delete(RUN_VIEW_PARAM);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, []);
  // What the page opens on Live (an agent's profile, a call in the trace) goes there first: Live stays mounted under
  // Progress, hidden, and shows once the page has drawn it.
  const viewRef = useRef(view);
  useEffect(() => {
    viewRef.current = view;
  }, [view]);
  const showInLive = useCallback(
    (tab: SessionTab) => {
      if (viewRef.current === "live") return showSession(tab);
      choose("live", null);
      requestAnimationFrame(() => showSession(tab));
    },
    [showSession, choose],
  );
  return [offered ? view : "live", choose, showInLive] as const;
}

const TAB =
  "inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-3 text-sm leading-5 font-medium whitespace-nowrap transition-colors duration-[var(--transition-duration-fast)] focus-visible:outline-2 focus-visible:outline-ring max-md:min-h-11 max-md:flex-1 max-md:justify-center";

/**
 * Live, Progress and Diff under the run's head. Live and Progress are views of this page; Diff is the page of the run's
 * diff (its own route), offered once the worker uploaded one.
 */
export function RunViewTabs({ view, onChange, diffHref }: { view: RunView; onChange: (view: RunView) => void; diffHref: Route | null }) {
  const t = useTranslations("runs.detail.views");
  const item = (name: RunView, label: string, Icon: typeof Radio) => (
    <button
      type="button"
      onClick={() => onChange(name)}
      aria-current={view === name ? "page" : undefined}
      className={cn(TAB, view === name ? "bg-foreground text-background" : "text-muted-foreground hover:bg-accent hover:text-foreground")}
      data-testid={`run-view-${name}`}
    >
      <Icon className="size-4" aria-hidden="true" />
      {label}
    </button>
  );
  return (
    <nav aria-label={t("label")} className="flex min-w-0 gap-1 rounded-lg bg-muted/60 p-1 md:self-start" data-testid="run-views">
      {item("live", t("live"), Radio)}
      {item("progress", t("progress"), ListChecks)}
      {diffHref ? (
        <Link href={diffHref} className={cn(TAB, "text-muted-foreground hover:bg-accent hover:text-foreground")} data-testid="run-view-diff-tab">
          <FileDiff className="size-4" aria-hidden="true" />
          {t("diff")}
        </Link>
      ) : null}
    </nav>
  );
}
