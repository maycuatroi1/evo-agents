import { KgAnswersSkeleton } from "@/components/kg/answers/kg-answers-page";
import { LoadingState } from "@/components/states/states";

export default function Loading() {
  return (
    <LoadingState>
      <KgAnswersSkeleton />
    </LoadingState>
  );
}
