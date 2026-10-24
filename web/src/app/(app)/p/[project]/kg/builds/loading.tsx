import { KgBuildsSkeleton } from "@/components/kg/kg-builds";
import { LoadingState } from "@/components/states/states";

export default function Loading() {
  return (
    <LoadingState>
      <KgBuildsSkeleton />
    </LoadingState>
  );
}
