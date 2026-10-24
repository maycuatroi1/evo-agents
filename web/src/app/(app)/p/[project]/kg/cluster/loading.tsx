import { LoadingState, PageSkeleton } from "@/components/states/states";

export default function Loading() {
  return (
    <LoadingState>
      <PageSkeleton />
    </LoadingState>
  );
}
