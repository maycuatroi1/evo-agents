import { Skeleton } from "@/components/ui/skeleton";

import { READING_COLUMN } from "./message-item";

export const CARD = "flex min-h-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised";

/** A chat while its first page loads: a head, three messages and the composer, as grey shapes. */
export function ChatLoading() {
  return (
    <div className={`${CARD} h-full w-full`} data-testid="chat-loading">
      <div className="flex items-center gap-3 border-b px-4 py-2">
        <Skeleton className="size-8 shrink-0 rounded-full" />
        <Skeleton className="h-5 w-48 rounded-xs" />
      </div>
      <div className="flex flex-1 flex-col gap-5 p-4">
        {[0, 1, 2].map((row) => (
          <div key={row} className="flex gap-3">
            <Skeleton className="size-8 shrink-0 rounded-full" />
            <div className="flex flex-1 flex-col gap-1.5">
              <Skeleton className="h-3.5 w-40 rounded-xs" />
              <Skeleton className="h-4 w-3/4 rounded-xs" />
            </div>
          </div>
        ))}
      </div>
      <div className="px-3 pt-1 pb-3">
        <Skeleton className={`h-12 rounded-lg ${READING_COLUMN}`} />
      </div>
    </div>
  );
}
