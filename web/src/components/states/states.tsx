"use client";

import { FileQuestion, type LucideIcon, SearchX, ServerCrash, ShieldX, TriangleAlert, WifiOff } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { useInCard } from "@/components/data/data-card";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { type ApiErrorInfo, errorKind } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/**
 * The loading, empty and error states every data view uses (the kit's EmptyState). Each state is a landmark-free
 * block with a heading, so it reads the same to a screen reader wherever it appears; colour is never the only signal
 * (icon and text). Alone a state is a card; inside a `DataCard` (a list's card, under its toolbar) it draws no frame.
 */

type StateProps = {
  icon: LucideIcon;
  title: string;
  description?: ReactNode;
  tone?: "neutral" | "danger" | "warning";
  children?: ReactNode;
  className?: string;
  testId?: string;
};

const TONES = {
  neutral: "bg-surface-sunken text-muted-foreground",
  danger: "bg-danger-soft text-danger",
  warning: "bg-attention-soft text-attention",
} as const;

/**
 * The icon in a 40 px `surface-sunken` square, a one-line title (15 px, 600), one sentence in `fg-muted` and the
 * actions, the primary one first, centred with 48 px above and below.
 */
export function StatePanel({ icon: Icon, title, description, tone = "neutral", children, className, testId }: StateProps) {
  const inCard = useInCard();
  return (
    <section
      data-testid={testId}
      className={cn(
        "flex w-full min-w-0 flex-col items-center gap-2 px-6 py-12 text-center",
        !inCard && "rounded-md border bg-card shadow-raised",
        className,
      )}
    >
      <span className={cn("mb-2 grid size-10 shrink-0 place-items-center rounded-md", TONES[tone])} aria-hidden="true">
        <Icon className="size-5" />
      </span>
      <h2 className="max-w-full text-[15px] leading-[22px] font-semibold text-balance text-foreground [overflow-wrap:anywhere]">
        {title}
      </h2>
      {description ? (
        <div className="max-w-[46ch] text-[13px] leading-[18px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">
          {description}
        </div>
      ) : null}
      {children ? <div className="mt-3 flex flex-wrap items-center justify-center gap-2">{children}</div> : null}
    </section>
  );
}

/** First use: what the page is for, said once, and the one thing to do next. */
export function EmptyState(props: Omit<StateProps, "tone">) {
  return <StatePanel tone="neutral" testId="state-empty" {...props} />;
}

/** A filter in force, as the no-results state names it: "State" and "Active", "Search" and the query. */
export type ActiveFilter = { label: string; value: string };

/**
 * No row matches: the filters in force, each as a tag ("State: Active", "Search: credentials"), and Clear filters,
 * which puts the list back to everything.
 */
export function NoResults({
  title,
  filters,
  onClear,
  testId = "state-empty",
}: {
  title: string;
  filters: ActiveFilter[];
  onClear: () => void;
  testId?: string;
}) {
  const t = useTranslations("states.noResults");
  return (
    <StatePanel
      icon={SearchX}
      title={title}
      testId={testId}
      description={
        filters.length > 0 ? (
          <>
            <p>{t("inUse")}</p>
            <ul className="mt-2 flex flex-wrap justify-center gap-1.5" data-testid="filters-in-use">
              {filters.map((filter) => (
                <li
                  key={`${filter.label}:${filter.value}`}
                  className="inline-flex h-[22px] max-w-full min-w-0 items-center gap-1 rounded-xs bg-surface-sunken px-1.5 text-xs leading-4"
                >
                  <span className="shrink-0 text-muted-foreground">{t("filter", { label: filter.label })}</span>
                  <span className="truncate font-medium text-foreground">{filter.value}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null
      }
    >
      <Button variant="outline" onClick={onClear} data-testid="clear-filters">
        {t("clear")}
      </Button>
    </StatePanel>
  );
}

function BackHome() {
  const t = useTranslations("states");
  return (
    <Button asChild variant="outline">
      <Link href="/">{t("backHome")}</Link>
    </Button>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <span>{label}:</span>
      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground break-all">{children}</code>
    </div>
  );
}

export function ForbiddenState({ error }: { error?: ApiErrorInfo }) {
  const t = useTranslations("states");
  return (
    <StatePanel
      icon={ShieldX}
      tone="warning"
      title={t("forbiddenTitle")}
      description={t("forbiddenDescription")}
      testId="state-forbidden"
    >
      <BackHome />
      {error?.requestId ? (
        <div className="basis-full">
          <Detail label={t("requestId")}>{error.requestId}</Detail>
        </div>
      ) : null}
    </StatePanel>
  );
}

export function NotFoundState({ title, description }: { title?: string; description?: string }) {
  const t = useTranslations("states");
  return (
    <StatePanel
      icon={FileQuestion}
      title={title ?? t("notFoundTitle")}
      description={description ?? t("notFoundDescription")}
      testId="state-not-found"
    >
      <BackHome />
    </StatePanel>
  );
}

/** A 5xx, no answer, or an unexpected 4xx: what failed, the request id to report, and a retry. */
export function ServerErrorState({ error, onRetry }: { error: ApiErrorInfo; onRetry?: () => void }) {
  const t = useTranslations("states");
  const kind = errorKind(error.status);
  const [icon, title, description] =
    kind === "network"
      ? [WifiOff, t("networkErrorTitle"), t("networkErrorDescription")]
      : kind === "server"
        ? [ServerCrash, t("serverErrorTitle"), t("serverErrorDescription")]
        : [TriangleAlert, t("clientErrorTitle"), error.message];
  return (
    <StatePanel icon={icon} tone="danger" title={title} description={description} testId="state-error">
      {onRetry ? (
        <Button onClick={onRetry}>
          {t("retry")}
        </Button>
      ) : null}
      <BackHome />
      <div className="flex basis-full flex-col gap-1.5 pt-2">
        {error.requestId ? <Detail label={t("requestId")}>{error.requestId}</Detail> : null}
        {error.status ? <Detail label={t("status", { status: error.status })}>{error.code}</Detail> : null}
      </div>
    </StatePanel>
  );
}

/** The state for any failed API call except 401, which never reaches a page (it goes to the sign-in page). */
export function ApiErrorState({ error, onRetry }: { error: ApiErrorInfo; onRetry?: () => void }) {
  const kind = errorKind(error.status);
  if (kind === "forbidden") return <ForbiddenState error={error} />;
  if (kind === "not_found") return <NotFoundState />;
  return <ServerErrorState error={error} onRetry={onRetry} />;
}

/**
 * Announced once to screen readers; the skeleton itself is decoration, and it shows only 300 ms after loading starts
 * so a quick answer never flashes it.
 */
export function LoadingState({ children, className }: { children?: ReactNode; className?: string }) {
  const t = useTranslations("states");
  return (
    <div role="status" aria-live="polite" aria-busy="true" className={className} data-testid="state-loading">
      <span className="sr-only">{t("loading")}</span>
      <div aria-hidden="true" className="animate-appear-late">
        {children ?? <PageSkeleton />}
      </div>
    </div>
  );
}

export function PageSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <Skeleton className="h-7 w-56" />
        <Skeleton className="h-4 w-full max-w-md" />
      </div>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {Array.from({ length: 3 }, (_, i) => (
          <Skeleton key={i} className="h-32 w-full rounded-md" />
        ))}
      </div>
      <TableSkeleton rows={5} />
    </div>
  );
}

/** Bar widths that vary from row to row, so the skeleton reads as rows of text rather than stripes. */
const TITLE_WIDTHS = ["w-[62%]", "w-[48%]", "w-[70%]", "w-[55%]", "w-[40%]", "w-[66%]"];
const SUB_WIDTHS = ["w-[38%]", "w-[30%]", "w-[44%]", "w-[26%]", "w-[34%]", "w-[40%]"];

/** A list's toolbar while it loads: the search field and three chips; on a phone, the 44 px field alone. */
function ToolbarSkeleton() {
  return (
    <div className="flex flex-wrap items-center gap-2 border-b px-4 py-3">
      <Skeleton className="h-8 w-full rounded-sm max-md:h-11 sm:w-64" />
      {[56, 72, 64].map((width) => (
        <Skeleton key={width} className="h-7 rounded-sm max-md:hidden" style={{ width }} />
      ))}
    </div>
  );
}

/**
 * A table in the shape of the kit's rows: the header on `surface-sunken`, then 44 px rows of a short reference, a
 * title over its secondary line, a state pill and a figure on the right; on a phone, the list's 60 px rows of a title
 * over its line, with no header. `toolbar` adds the toolbar above. Alone it is a card; inside a `DataCard` it draws no
 * frame.
 */
export function TableSkeleton({ rows = 5, toolbar = false }: { rows?: number; toolbar?: boolean }) {
  const inCard = useInCard();
  return (
    <div className={cn("min-w-0", !inCard && "overflow-hidden rounded-md border bg-card shadow-raised")}>
      {toolbar ? <ToolbarSkeleton /> : null}
      <div className="flex h-9 items-center gap-6 border-b bg-surface-sunken px-4 max-md:hidden">
        {[40, 120, 56].map((width) => (
          <span key={width} className="h-2.5 rounded-xs bg-border" style={{ width }} />
        ))}
      </div>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex h-11 items-center gap-4 border-b px-4 last:border-b-0 max-md:h-[60px]">
          <Skeleton className="h-3 w-8 shrink-0 rounded-xs max-md:hidden" />
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <Skeleton className={cn("h-3 rounded-xs", TITLE_WIDTHS[i % TITLE_WIDTHS.length])} />
            <Skeleton className={cn("h-2.5 rounded-xs", SUB_WIDTHS[i % SUB_WIDTHS.length])} />
          </div>
          <Skeleton className="h-[22px] w-20 shrink-0 rounded-full" />
          <Skeleton className="hidden h-3 w-14 shrink-0 rounded-xs md:block" />
        </div>
      ))}
    </div>
  );
}

/**
 * A list page while it loads: the strip of counts above the list (`metrics` cells of MetricStrip, the same 78 px: a
 * 16 px label, a 30 px figure and a 16 px line), then the list's card.
 */
export function ListSkeleton({ rows = 6, metrics = 0 }: { rows?: number; metrics?: number }) {
  return (
    <div className="flex flex-col gap-6">
      {metrics > 0 ? (
        <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border bg-border shadow-raised lg:grid-cols-4">
          {Array.from({ length: metrics }, (_, i) => (
            <div key={i} className="flex h-[78px] flex-col justify-between bg-card px-4 py-3">
              <Skeleton className="h-3 w-20 rounded-xs" />
              <Skeleton className="h-6 w-10 rounded-xs" />
              <Skeleton className="h-2.5 w-28 rounded-xs" />
            </div>
          ))}
        </div>
      ) : null}
      <TableSkeleton rows={rows} toolbar />
    </div>
  );
}
