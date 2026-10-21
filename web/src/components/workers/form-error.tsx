"use client";

import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

/**
 * What the hub refused, in the owner's Settings card, next to what was tried: the same alert as a dialog's inline
 * error. `title` names what could not be done when the reason alone would not say it ("Couldn't lock the version").
 */
export function FormError({
  title,
  text,
  detail,
  requestId,
  testId,
}: {
  title?: string;
  text: string;
  detail: string | null;
  requestId: string | null;
  testId: string;
}) {
  const t = useTranslations("workers.settings");
  return (
    <div
      role="alert"
      data-testid={testId}
      className="flex items-start gap-2.5 rounded-md border border-danger/30 bg-danger-soft px-3 py-2.5 text-sm text-danger"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1">
        {title ? <p className="font-medium text-pretty">{title}</p> : null}
        <p className={title ? "text-pretty" : "font-medium text-pretty"}>{text}</p>
        {detail ? <p className="text-xs break-words">{detail}</p> : null}
        {requestId ? <p className="font-mono text-xs break-all">{t("requestId", { id: requestId })}</p> : null}
      </div>
    </div>
  );
}
