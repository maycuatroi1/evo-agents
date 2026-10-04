import type { ReactNode } from "react";

/** The page's one h1, its summary, and what sits beside them (badges, counts). */
export function PageHeader({
  title,
  description,
  meta,
  eyebrow,
}: {
  title: ReactNode;
  description?: ReactNode;
  meta?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-3 border-b pb-5 sm:flex-row sm:items-end sm:justify-between">
      <div className="flex min-w-0 flex-col gap-1.5">
        {eyebrow ? <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{eyebrow}</p> : null}
        <h1 className="text-2xl font-semibold tracking-tight break-words text-balance">{title}</h1>
        {description ? <p className="max-w-3xl text-sm text-pretty text-muted-foreground">{description}</p> : null}
      </div>
      {meta ? <div className="flex shrink-0 flex-wrap items-center gap-2">{meta}</div> : null}
    </header>
  );
}
