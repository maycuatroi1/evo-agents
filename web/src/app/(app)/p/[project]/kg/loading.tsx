import { KgOverviewSkeleton } from "@/components/kg/kg-overview";
import { LoadingState } from "@/components/states/states";

export default function Loading() {
  return (
    <LoadingState>
      <KgOverviewSkeleton />
    </LoadingState>
  );
}
