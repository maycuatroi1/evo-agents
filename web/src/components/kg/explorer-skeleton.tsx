import { Skeleton } from "@/components/ui/skeleton";

/**
 * The explorer's skeleton while the route loads: the head, the tabs and the frame. A module of its own, since the
 * loading state of /p/{project}/kg also stands for the pages below it, which must not load the explorer with it.
 */
export function KgExplorerSkeleton() {
  return (
    <div className="flex flex-col gap-4 lg:h-[calc(100dvh-6.25rem)]">
      <div className="flex flex-col gap-3">
        <Skeleton className="h-7 w-56" />
        <Skeleton className="h-10 w-80" />
      </div>
      <Skeleton className="h-[62vh] w-full rounded-md lg:h-auto lg:flex-1" />
    </div>
  );
}
