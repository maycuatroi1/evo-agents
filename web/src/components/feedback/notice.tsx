"use client";

import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { errorKind, isApiError } from "@/lib/api/errors";

/**
 * Failures of writes. A write's result is a toast (components/feedback/toast.tsx); a failure inside a dialog that stays
 * open is said here instead, next to the action that failed, until the dialog closes or the action is tried again.
 */

/** An inline error inside a dialog, next to the action that failed. */
export function InlineError({ text, detail, requestId }: { text: string; detail?: string | null; requestId?: string | null }) {
  const t = useTranslations("admin.notice");
  return (
    <div
      role="alert"
      data-testid="admin-dialog-error"
      className="flex items-start gap-2.5 rounded-md border border-danger/30 bg-danger-soft px-3 py-2.5 text-sm text-danger"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="font-medium text-pretty">{text}</p>
        {detail ? <p className="text-xs break-words">{detail}</p> : null}
        {requestId ? <p className="font-mono text-xs break-all">{t("requestId", { id: requestId })}</p> : null}
      </div>
    </div>
  );
}

export type WriteFailure = { text: string; detail: string | null; requestId: string | null; status: number };

type Overrides = Partial<Record<403 | 404 | 409 | 422, string>>;

/**
 * What to tell the admin when a write fails, by status. On a 401 the page is already on its way to the sign-in page.
 * The API's own message is English and kept as a detail for 422, where it names the field the hub refused.
 */
export function useWriteFailure() {
  const t = useTranslations("admin.errors");
  return useCallback(
    (error: unknown, overrides: Overrides = {}): WriteFailure => {
      const info = isApiError(error) ? error.info : { status: 0, code: "network", message: String(error), requestId: null };
      const kind = errorKind(info.status);
      const status = info.status;
      let text: string;
      let detail: string | null = null;
      if (status === 401) text = t("unauthorized");
      else if (status === 403) text = overrides[403] ?? t("forbidden");
      else if (status === 404) text = overrides[404] ?? t("conflict");
      else if (status === 409) text = overrides[409] ?? t("conflict");
      else if (status === 422 || kind === "client") {
        text = overrides[422] ?? t("invalid");
        detail = t("apiMessage", { message: info.message });
      } else if (kind === "network") text = t("network");
      else text = t("server");
      return { text, detail, requestId: info.requestId, status };
    },
    [t],
  );
}
