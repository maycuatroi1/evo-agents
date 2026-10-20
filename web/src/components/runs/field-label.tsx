import type { ReactNode } from "react";

import { InfoTip } from "@/components/feedback/info-tip";

/** A field's label, with an info icon beside it when there is more to say than its name. */
export function FieldLabel({ htmlFor, label, info, testId }: { htmlFor: string; label: string; info?: ReactNode; testId?: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <label htmlFor={htmlFor} className="text-sm font-medium">
        {label}
      </label>
      {info ? (
        <InfoTip label={label} testId={testId}>
          {info}
        </InfoTip>
      ) : null}
    </div>
  );
}
