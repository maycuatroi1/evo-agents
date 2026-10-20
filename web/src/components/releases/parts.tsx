"use client";

import { ExternalLink } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { safeUrl } from "./model";

/** A link inside a line of text: `brand` with a quiet underline, so colour is not all that sets it apart (axe). */
export const TEXT_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

/**
 * The kit's card with a head, as the Curator's pages draw theirs: a 48 px head (the h2, one line under it, what ends
 * it on the right) above its rows, a section named by its title.
 */
export function Panel({
  title,
  sub,
  status,
  action,
  children,
  className,
  testId,
}: {
  title: ReactNode;
  sub?: ReactNode;
  status?: ReactNode;
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
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <h2 id={headingId} className="text-[15px] leading-[22px] font-semibold">
              {title}
            </h2>
            {status}
          </div>
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

/**
 * "in 5 hours" or "3 minutes ago" from the browser's clock, ticking; the full time is its tooltip. The server and the
 * first render in the browser show the full time, so the two agree.
 */
export function Relative({ value, testId }: { value: string; testId?: string }) {
  const format = useFormatter();
  const now = useNow(true);
  const date = new Date(value);
  const full = format.dateTime(date, { dateStyle: "medium", timeStyle: "short" });
  return (
    <time dateTime={value} title={full} className="whitespace-nowrap tabular-nums" data-testid={testId}>
      {now === null ? full : format.relativeTime(date, now)}
    </time>
  );
}

/** A time as people read it, with the date: "Oct 22, 2026, 6:00 AM". */
export function FullTime({ value, testId }: { value: string; testId?: string }) {
  const format = useFormatter();
  return (
    <time dateTime={value} className="tabular-nums" data-testid={testId}>
      {format.dateTime(new Date(value), { dateStyle: "medium", timeStyle: "short" })}
    </time>
  );
}

/**
 * A link out of the hub (a workflow run, a pull request), opened in a new tab without an opener or a referrer, said to
 * screen readers; only http and https addresses are taken, anything else shows as text.
 */
export function OutLink({ href, children, className, testId }: { href: string | null | undefined; children: ReactNode; className?: string; testId?: string }) {
  const t = useTranslations("releases");
  const url = safeUrl(href);
  if (!url) return <span className={className}>{children}</span>;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className={cn(TEXT_LINK, "inline-flex items-center gap-1 max-md:py-2", className)} data-testid={testId}>
      {children}
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="sr-only">{t("newTab")}</span>
    </a>
  );
}

/** Paths as name chips that wrap: the packaging paths, the paths a check found changed. */
export function PathList({ paths, empty, testId }: { paths: readonly string[]; empty: ReactNode; testId?: string }) {
  if (paths.length === 0) return <span className="text-muted-foreground">{empty}</span>;
  return (
    <ul className="flex min-w-0 flex-wrap gap-1.5" data-testid={testId}>
      {paths.map((path) => (
        <li key={path} className="inline-flex h-5 max-w-full items-center rounded-xs bg-surface-sunken px-1.5 font-mono text-xs leading-4 text-muted-foreground">
          <span className="truncate">{path}</span>
        </li>
      ))}
    </ul>
  );
}
