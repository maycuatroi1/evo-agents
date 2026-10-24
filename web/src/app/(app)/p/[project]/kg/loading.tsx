import { KgExplorerSkeleton } from "@/components/kg/explorer-skeleton";
import { LoadingState } from "@/components/states/states";

export default function Loading() {
  return (
    <LoadingState>
      <KgExplorerSkeleton />
    </LoadingState>
  );
}
