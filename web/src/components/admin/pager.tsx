"use client";

import { ChevronLeft, ChevronRight, ChevronsLeft, Loader2 } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentProps } from "react";

import { Button } from "@/components/ui/button";

type PagerProps = {
  /** What is paged, for the navigation landmark's name. */
  label: string;
  page: number | null;
  count: number;
  hasNext: boolean;
  canGoBack: boolean;
  atStart: boolean;
  busy: boolean;
  onNext: () => void;
  onPrevious: () => void;
  onFirst: () => void;
};

/**
 * A button that cannot act right now but keeps keyboard focus: `disabled` would drop focus to the page body the
 * moment the last page loads under the visitor's "next" press.
 */
function PageButton({ unavailable, onClick, ...props }: ComponentProps<typeof Button> & { unavailable: boolean }) {
  return (
    <Button
      type="button"
      variant="outline"
      aria-disabled={unavailable || undefined}
      className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
      onClick={(event) => {
        if (!unavailable) onClick?.(event);
      }}
      {...props}
    />
  );
}

/** Cursor paging: previous and next, the page number when it is known, and the rows on this page. */
export function Pager({ label, page, count, hasNext, canGoBack, atStart, busy, onNext, onPrevious, onFirst }: PagerProps) {
  const t = useTranslations("admin.pager");
  if (atStart && !hasNext && !busy) return null; // one page: nothing to turn
  return (
    <nav aria-label={t("label", { what: label })} className="flex flex-wrap items-center justify-between gap-3" data-testid="pager">
      <p className="flex items-center gap-2 text-sm text-muted-foreground" aria-live="polite" aria-atomic="true">
        {busy ? (
          <>
            <Loader2 className="size-4 animate-spin motion-reduce:animate-none" aria-hidden="true" />
            {t("loading")}
          </>
        ) : (
          <>
            {page !== null ? (
              <span className="font-medium text-foreground" data-testid="pager-page">
                {t("page", { page })}
              </span>
            ) : null}
            <span>{t("rows", { count })}</span>
          </>
        )}
      </p>
      <div className="flex items-center gap-2">
        {canGoBack || atStart ? (
          <PageButton unavailable={atStart || busy} onClick={onPrevious} data-testid="pager-previous">
            <ChevronLeft aria-hidden="true" />
            {t("previous")}
          </PageButton>
        ) : (
          <PageButton unavailable={busy} onClick={onFirst} data-testid="pager-first">
            <ChevronsLeft aria-hidden="true" />
            {t("first")}
          </PageButton>
        )}
        <PageButton unavailable={!hasNext || busy} onClick={onNext} data-testid="pager-next">
          {t("next")}
          <ChevronRight aria-hidden="true" />
        </PageButton>
      </div>
    </nav>
  );
}
