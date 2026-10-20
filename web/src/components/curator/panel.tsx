"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/**
 * The card of the Curator's History, Graph health and Runtime pages: the kit's card with a 48 px head (the h2, one
 * line under it, an action on the right) above its rows, as Home's cards are; a section named by its title.
 */
export function Panel({
  title,
  sub,
  action,
  children,
  className,
  testId,
}: {
  title: string;
  sub?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  testId: string;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId} className={cn("@container min-w-0 rounded-md border bg-card shadow-raised", className)} data-testid={testId}>
      <div className="flex min-h-12 flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2">
        <div className="flex min-w-0 flex-1 basis-48 flex-col">
          <h2 id={headingId} className="text-[15px] leading-[22px] font-semibold">
            {title}
          </h2>
          {sub ? <p className="text-xs text-pretty text-fg-subtle">{sub}</p> : null}
        </div>
        {action ? <div className="flex flex-wrap items-center gap-2">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** One quiet line inside a panel: nothing to list, or why. */
export function PanelLine({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <p className="px-4 py-6 text-[13px] text-pretty text-muted-foreground" data-testid={testId}>
      {children}
    </p>
  );
}

/** A panel's rows while they load: a few bars of text. */
export function PanelSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="flex flex-col gap-3 px-4 py-4" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className={cn("h-3", index % 2 === 0 ? "w-2/3" : "w-1/2")} />
      ))}
    </div>
  );
}

/** A link in a panel's head or in a line of text: `brand` with a quiet underline, 44 px tall to a finger. */
export const PANEL_LINK =
  "rounded-xs text-[13px] font-medium text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current max-md:inline-flex max-md:min-h-11 max-md:items-center";

/** The head and the cells of a small table inside a panel, numbers on the right in tabular figures. */
export const TABLE_HEAD = "h-9 px-3 text-left text-xs font-medium whitespace-nowrap text-muted-foreground first:pl-4 last:pr-4";
export const TABLE_CELL = "h-10 px-3 text-[13px] text-muted-foreground first:pl-4 last:pr-4";
export const NUMBER = "text-right tabular-nums whitespace-nowrap";

/** A small table in its own focusable region, so a wide one scrolls inside the panel and never the page. */
export function PanelTable({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} className="overflow-x-auto focus-visible:outline-offset-[-2px]" data-testid={testId}>
      <table className="w-full border-collapse">
        <caption className="sr-only">{label}</caption>
        {children}
      </table>
    </div>
  );
}

// A button that cannot act right now keeps keyboard focus: `disabled` would drop it to the page's body as the page turns.
const UNAVAILABLE = "aria-disabled:cursor-not-allowed aria-disabled:opacity-50";

/**
 * Previous and Next under a paged list, with the rows this page shows of all of them ("21 to 40 of 132"), said again
 * in a polite live region when the page turns. Nothing when everything fits on one page.
 */
export function PrevNext({
  label,
  page,
  perPage,
  shown,
  total,
  busy,
  onPage,
  testId,
}: {
  label: string;
  page: number;
  perPage: number;
  shown: number;
  total: number;
  busy: boolean;
  onPage: (page: number) => void;
  testId: string;
}) {
  const t = useTranslations("curator.pages");
  const first = (page - 1) * perPage + 1;
  const hasNext = first - 1 + shown < total;
  if (page <= 1 && !hasNext) return null;
  return (
    <nav aria-label={t("label", { what: label })} className="flex flex-wrap items-center justify-between gap-3" data-testid={testId}>
      <p className="text-[13px] text-muted-foreground tabular-nums" aria-live="polite" aria-atomic="true">
        {busy ? t("loading") : t("range", { first, last: first - 1 + shown, total })}
      </p>
      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-disabled={page <= 1 || busy || undefined}
          className={UNAVAILABLE}
          onClick={() => {
            if (page > 1 && !busy) onPage(page - 1);
          }}
          data-testid={`${testId}-previous`}
        >
          <ChevronLeft aria-hidden="true" />
          {t("previous")}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-disabled={!hasNext || busy || undefined}
          className={UNAVAILABLE}
          onClick={() => {
            if (hasNext && !busy) onPage(page + 1);
          }}
          data-testid={`${testId}-next`}
        >
          {t("next")}
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
    </nav>
  );
}
