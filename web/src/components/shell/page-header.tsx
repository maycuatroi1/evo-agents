import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * The head of a page, one row as the UI kit draws it (web/DESIGN.md, Layout): the page's one h1, the state of what it
 * names (`status`, a `StatusBadge`), its tags and chips (`tags`: a kind, a role, an identifier, a count), then the
 * page's actions on the right. `sub` is one optional line under the row that links the parent objects (a run's plan
 * and step, a step's plan) or says where the object stands. There is no eyebrow, no description paragraph and no rule
 * under the head: what a page is for is said once, in its empty state.
 *
 * The row wraps instead of squeezing: when the actions do not fit beside the title they move to a line of their own,
 * still on the right, and a long title wraps inside its own group; so nothing pushes the page sideways at 375 px.
 */
export function PageHeader({
  title,
  status,
  tags,
  actions,
  sub,
  className,
  testId,
}: {
  title: ReactNode;
  status?: ReactNode;
  tags?: ReactNode;
  actions?: ReactNode;
  sub?: ReactNode;
  className?: string;
  testId?: string;
}) {
  return (
    <header className={cn("flex flex-col gap-1.5", className)} data-testid={testId}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <div className="flex max-w-full min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
          <h1 className="min-w-0 text-xl font-semibold tracking-tight text-balance [overflow-wrap:anywhere]">{title}</h1>
          {status}
          {tags}
        </div>
        {actions ? (
          <div className="ml-auto flex max-w-full min-w-0 flex-wrap items-center justify-end gap-2" data-slot="page-actions">
            {actions}
          </div>
        ) : null}
      </div>
      {sub ? (
        <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground [overflow-wrap:anywhere]" data-slot="page-sub">
          {sub}
        </p>
      ) : null}
    </header>
  );
}
