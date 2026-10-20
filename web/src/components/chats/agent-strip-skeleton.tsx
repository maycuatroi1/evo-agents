import { useTranslations } from "next-intl";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

/** The row of agents' frame: one line that scrolls sideways on its own, under the list's head. */
export const STRIP_ROW = "flex min-w-0 shrink-0 gap-1 overflow-x-auto overscroll-x-contain px-2 py-2 [scrollbar-width:thin]";

/** The row of agents while its code or the agents load: five rounds where they will be. */
export function AgentStripSkeleton() {
  const t = useTranslations("chats.strip");
  return (
    <div className={cn(STRIP_ROW, "border-b")} aria-busy="true" data-testid="agent-strip" data-state="loading">
      <span className="sr-only">{t("loading")}</span>
      {[0, 1, 2, 3, 4].map((item) => (
        <span key={item} className="flex w-18 shrink-0 flex-col items-center gap-1.5 py-1.5" aria-hidden="true">
          <Skeleton className="size-10 rounded-full" />
          <Skeleton className="h-2.5 w-10 rounded-xs" />
        </span>
      ))}
    </div>
  );
}
