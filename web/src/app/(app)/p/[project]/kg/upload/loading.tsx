import { KgUploadSkeleton } from "@/components/kg/upload/kg-upload-page";
import { LoadingState } from "@/components/states/states";

export default function Loading() {
  return (
    <LoadingState>
      <KgUploadSkeleton />
    </LoadingState>
  );
}
