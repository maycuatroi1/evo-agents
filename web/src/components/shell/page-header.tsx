import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The page's one h1, its summary, and what sits beside them (badges, counts, actions). `metaBelow` is for a page whose
 * meta holds several actions (a run's controls): it sits under the title, wrapping as it needs, until the xl
 * breakpoint, where there is room for both on one line.
 */
export function PageHeader({
  title,
  description,
  meta,
  eyebrow,
  metaBelow = false,
}: {
  title: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  eyebrow?: ReactNode;
  metaBelow?: boolean;
}) {
  return (
    <header
      className={cn(
        "flex flex-col gap-3 border-b pb-5",
        metaBelow ? "xl:flex-row xl:items-end xl:justify-between" : "sm:flex-row sm:items-end sm:justify-between",
      )}
    >
      <div className="flex min-w-0 flex-col gap-1.5">
        {eyebrow ? <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{eyebrow}</p> : null}
        <h1 className="text-2xl font-semibold tracking-tight break-words text-balance">{title}</h1>
        {description ? <p className="max-w-3xl text-sm text-pretty text-muted-foreground">{description}</p> : null}
      </div>
      {meta ? (
        <div className={cn("flex flex-wrap items-center gap-2", metaBelow ? "min-w-0 xl:shrink-0 xl:justify-end" : "shrink-0")}>{meta}</div>
      ) : null}
    </header>
  );
}
