"use client";

import type { LucideIcon } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";

import { cn } from "@/lib/utils";

/** A tab; `count` shows beside its label, and `countLabel` says it to screen readers ("2 open"). */
export type TabLink = { id: string; label: string; href: Route; icon: LucideIcon; count?: number | null; countLabel?: string };

/**
 * Tabs under a page head, as a plan's (plans/plan-header.tsx): links in a labelled nav, the current one underlined in
 * `brand` and named by aria-current, 44 px tall, scrolling sideways inside their own row when they do not fit.
 */
export function TabNav({ label, tabs, current, testId }: { label: string; tabs: TabLink[]; current: string | null; testId?: string }) {
  return (
    <nav aria-label={label} className="-mb-px flex gap-1 overflow-x-auto border-b" data-testid={testId}>
      {tabs.map((tab) => {
        const active = tab.id === current;
        return (
          <Link
            key={tab.id}
            href={tab.href}
            aria-current={active ? "page" : undefined}
            data-testid={`srs-tab-${tab.id}`}
            className={cn(
              "inline-flex min-h-11 items-center gap-2 border-b-2 px-3 text-sm font-medium whitespace-nowrap transition-colors",
              active ? "border-brand text-foreground" : "border-transparent text-muted-foreground hover:border-input hover:text-foreground",
            )}
          >
            <tab.icon className="size-4" aria-hidden="true" />
            {tab.label}
            {tab.count ? (
              <>
                <span className="rounded-full bg-muted px-1.5 text-xs leading-5 text-muted-foreground tabular-nums" aria-hidden="true">
                  {tab.count}
                </span>
                {tab.countLabel ? <span className="sr-only">{tab.countLabel}</span> : null}
              </>
            ) : null}
          </Link>
        );
      })}
    </nav>
  );
}
