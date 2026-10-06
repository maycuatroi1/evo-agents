"use client";

import { CircleCheck, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { Button } from "@/components/ui/button";
import { errorKind, isApiError } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

/**
 * The result of an admin write, shown on the page that made it. A success goes into a live region that is always in
 * the page, so screen readers announce it; a failure is an alert. Either stays until dismissed or replaced, so
 * nobody has to read it against a timer.
 */
export type Notice = { tone: "success" | "error"; text: string; detail?: string | null; requestId?: string | null };

export function useNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);
  const clear = useCallback(() => setNotice(null), []);
  return { notice, show: setNotice, clear };
}

export function NoticeArea({
  notice,
  onDismiss,
  emptyClassName = "empty:-mt-6",
}: {
  notice: Notice | null;
  onDismiss: () => void;
  /** Gives back the gap of the column it sits in while empty: a page's 24 px by default. */
  emptyClassName?: string;
}) {
  const success = notice?.tone === "success" ? notice : null;
  const failure = notice?.tone === "error" ? notice : null;
  return (
    <>
      {/* Always in the page, so a success is announced; while empty it gives back the gap the column put above it. */}
      <div role="status" aria-live="polite" aria-atomic="true" className={emptyClassName} data-testid="admin-notice-status">
        {success ? <NoticeBox notice={success} onDismiss={onDismiss} /> : null}
      </div>
      {failure ? (
        <div role="alert" data-testid="admin-notice-alert">
          <NoticeBox notice={failure} onDismiss={onDismiss} />
        </div>
      ) : null}
    </>
  );
}

function NoticeBox({ notice, onDismiss }: { notice: Notice; onDismiss: () => void }) {
  const t = useTranslations("admin.notice");
  const Icon = notice.tone === "success" ? CircleCheck : TriangleAlert;
  return (
    <div
      data-tone={notice.tone}
      className={cn(
        "flex items-start gap-3 rounded-md border px-3 py-2.5 text-sm",
        notice.tone === "success"
          ? "border-success/20 bg-success-soft text-success"
          : "border-danger/30 bg-card text-danger",
      )}
    >
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="font-medium text-pretty">{notice.text}</p>
        {notice.detail ? <p className="text-xs break-words opacity-90">{notice.detail}</p> : null}
        {notice.requestId ? (
          <p className="font-mono text-xs break-all opacity-90">{t("requestId", { id: notice.requestId })}</p>
        ) : null}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="-my-1 -mr-1 shrink-0 text-current hover:bg-black/5 dark:hover:bg-white/10"
        onClick={onDismiss}
        aria-label={t("dismiss")}
      >
        <X aria-hidden="true" />
      </Button>
    </div>
  );
}

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
