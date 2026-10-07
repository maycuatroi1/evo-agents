"use client";

import { ChevronRight, type LucideIcon } from "lucide-react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import Link from "next/link";
import { type ReactNode, useId } from "react";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** The sparkline's skeleton: the same 22 px, so the cell does not move when Recharts arrives. */
function SparklineSkeleton() {
  return <Skeleton className="h-full w-full rounded-xs" />;
}

const Sparkline = dynamic(() => import("./sparkline"), { ssr: false, loading: SparklineSkeleton });

export type MetricTone = "running" | "attention";

export type Metric = {
  /** The cell's test id, `summary-<id>`. */
  id: string;
  label: string;
  icon: LucideIcon;
  value: number;
  /** The figure as it reads, when not the bare value: a count with group separators, a size ("1.2 GB"). */
  display?: string;
  /** The page behind the number: the label links there, and the link covers the cell. */
  href?: Route;
  /** Words set small after the value ("and 1 lost"). */
  extra?: string | null;
  /** One line naming what is behind the number ("#13 on M1s-Mac-mini"). */
  meta?: ReactNode;
  /** A cell that needs a person (attention) or counts agents at work (running) sets a value above zero in its tone. */
  tone?: MetricTone;
  /** The running cell: a pulsing dot instead of the icon while its value is above zero. */
  live?: boolean;
  /** Work in flight: when every such cell is zero, a strip given `quiet` shows that line instead. */
  inFlight?: boolean;
  /** A count over time, oldest first, with its label naming every value ("Runs done per day, Oct 1 to Oct 7: 0, 2, 5"). */
  series?: { values: readonly number[]; label: string } | null;
};

/** Columns by number of cells: one row from the lg breakpoint, two columns below, the odd last cell across both. */
const GRID: Record<number, string> = {
  1: "grid-cols-1",
  2: "grid-cols-2",
  3: "grid-cols-1 sm:grid-cols-3",
  4: "grid-cols-2 lg:grid-cols-4",
  5: "grid-cols-2 lg:grid-cols-5 [&>*:last-child]:max-lg:col-span-2",
};

const TONE_VALUE: Record<MetricTone, string> = {
  running: "text-running",
  attention: "text-attention",
};

function MetricCell({ metric }: { metric: Metric }) {
  const { icon: Icon, value, tone, href } = metric;
  const zero = value === 0;
  const ids = useId();
  const described = [`${ids}-value`, metric.meta ? `${ids}-meta` : ""].filter(Boolean).join(" ");
  return (
    <div
      className={cn(
        "flex min-w-0 flex-col gap-0.5 bg-card px-4 py-3",
        // A cell with a page: its label's link covers it, so a click or tap anywhere in the cell opens the page.
        href && "group/metric relative transition-colors hover:bg-accent",
      )}
      data-testid={`summary-${metric.id}`}
      data-value={value}
    >
      <dt className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
        {metric.live ? (
          <span className="relative inline-flex size-2 shrink-0" aria-hidden="true">
            <span className={cn("size-2 rounded-full", zero ? "bg-neutral-solid" : "bg-running")} />
            {zero ? null : <span className="absolute inset-0 animate-live-ping rounded-full bg-running" />}
          </span>
        ) : (
          <Icon className="size-3.5 shrink-0" aria-hidden="true" />
        )}
        {href ? (
          <Link
            href={href}
            aria-describedby={described}
            className={cn(
              "truncate group-hover/metric:text-foreground",
              "after:absolute after:inset-0 after:content-[''] focus-visible:outline-none",
              "focus-visible:after:outline-2 focus-visible:after:-outline-offset-2 focus-visible:after:outline-ring",
            )}
            data-testid={`summary-${metric.id}-link`}
          >
            {metric.label}
          </Link>
        ) : (
          <span className="truncate">{metric.label}</span>
        )}
        {href ? (
          <ChevronRight className="ml-auto size-3.5 shrink-0 text-fg-subtle group-hover/metric:text-foreground" aria-hidden="true" />
        ) : null}
      </dt>
      <dd
        id={`${ids}-value`}
        className={cn(
          "flex items-baseline gap-2 text-2xl leading-[30px] font-semibold tracking-[-0.01em] tabular-nums",
          zero ? "text-fg-subtle" : tone ? TONE_VALUE[tone] : "text-foreground",
        )}
        data-testid={`summary-${metric.id}-value`}
      >
        {metric.display ?? value}
        {metric.extra ? <small className="truncate text-[13px] leading-[18px] font-normal tracking-normal text-fg-subtle">{metric.extra}</small> : null}
      </dd>
      {metric.series ? (
        <dd className="mt-1" data-testid={`summary-${metric.id}-series`}>
          <div role="img" aria-label={metric.series.label} className="h-[22px] w-full">
            <div aria-hidden="true" className="h-full w-full">
              <Sparkline values={metric.series.values} />
            </div>
          </div>
        </dd>
      ) : null}
      {metric.meta ? (
        <dd id={`${ids}-meta`} className="truncate text-xs text-fg-subtle">
          {metric.meta}
        </dd>
      ) : null}
    </div>
  );
}

/**
 * The kit's MetricStrip (web/DESIGN.md, Components): a row of counts in one container, cells divided by 1 px rules, a
 * zero in `fg-subtle`, a cell that needs a person in `attention` and the agents at work in `running` with a live dot,
 * and an optional sparkline per cell. A cell given `href` opens that page from anywhere in it. When `quiet` is given and every in-flight cell is zero, the strip gives way to
 * that one line ("All quiet."), so a calm project is not four zero tiles.
 */
export function MetricStrip({
  label,
  metrics,
  quiet,
  testId,
}: {
  label: string;
  metrics: readonly Metric[];
  quiet?: ReactNode;
  testId?: string;
}) {
  const inFlight = metrics.filter((metric) => metric.inFlight);
  const calm = quiet !== undefined && inFlight.length > 0 && inFlight.every((metric) => metric.value === 0);
  return (
    <section aria-label={label} data-testid={testId} data-quiet={calm || undefined}>
      {calm ? (
        quiet
      ) : (
        <dl className={cn("grid gap-px overflow-hidden rounded-md border bg-border shadow-raised", GRID[metrics.length] ?? GRID[4])}>
          {metrics.map((metric) => (
            <MetricCell key={metric.id} metric={metric} />
          ))}
        </dl>
      )}
    </section>
  );
}

/**
 * The line a calm strip shows instead (the kit's info banner): an icon, "All quiet." and one sentence in `fg-muted`, and
 * the next thing to do on the right.
 */
export function QuietLine({ icon: Icon, title, children, action, testId }: { icon: LucideIcon; title: string; children: ReactNode; action?: ReactNode; testId?: string }) {
  return (
    <div
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] leading-[18px]"
      data-testid={testId}
    >
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <p className="min-w-0 flex-1 text-pretty text-muted-foreground">
        <span className="font-semibold text-foreground">{title}</span> {children}
      </p>
      {action ? <div className="flex shrink-0 flex-wrap items-center gap-2">{action}</div> : null}
    </div>
  );
}
