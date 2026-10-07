"use client";

import { CircleCheck, CircleX, Info, LoaderCircle, type LucideIcon, X } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { toast } from "sonner";

import type { WriteFailure } from "@/components/admin/notice";
import { Button } from "@/components/ui/button";
import { Toaster } from "@/components/ui/sonner";
import { cn } from "@/lib/utils";

/**
 * The result of a write, as the kit's Toast (web/DESIGN.md, Toasts): bottom right, a past-tense title ("Run #13
 * dispatched"), one sentence, and a link back to what the action changed. A success or a note closes after 5 seconds
 * (the timer pauses while the pointer is over it or the tab is hidden); a failure stays until it is dismissed, says
 * what to do and carries the request id. Toasts sit in Sonner's polite live region, kept readable while a dialog is
 * open; a failure is also an alert.
 */
export type ToastTone = "success" | "info" | "error" | "running";

export type ToastLink = { label: string; href: Route };

export type Notice = {
  tone: ToastTone;
  /** The title: what happened, in the past tense, or for a failure what could not be done ("Couldn't rerun #4"). */
  text: string;
  /** One sentence under the title: what it means now, or for a failure what happened and what to do. */
  description?: string | null;
  /** The hub's own message, for a failure the hub refused. */
  detail?: string | null;
  requestId?: string | null;
  /** The way back to what the action changed; left out while that page is the one shown. */
  link?: ToastLink | null;
  /** A toast with the same id is replaced: a running toast becomes its result. */
  id?: string;
};

/** How long a success or a note stays, as the kit says. */
export const TOAST_MS = 5_000;

const LOOK: Record<ToastTone, { icon: LucideIcon; className: string }> = {
  success: { icon: CircleCheck, className: "text-success" },
  info: { icon: Info, className: "text-muted-foreground" },
  error: { icon: CircleX, className: "text-danger" },
  running: { icon: LoaderCircle, className: "animate-spin text-running" },
};

function samePage(href: string, pathname: string): boolean {
  return href.split(/[?#]/)[0] === pathname;
}

function ToastCard({ id, notice }: { id: string | number; notice: Notice }) {
  const t = useTranslations("toast");
  const pathname = usePathname();
  const { icon: Icon, className } = LOOK[notice.tone];
  const link = notice.link && !samePage(notice.link.href, pathname) ? notice.link : null;
  return (
    <div
      role={notice.tone === "error" ? "alert" : undefined}
      className="flex w-full items-start gap-3 rounded-md border bg-popover py-3 pr-2 pl-4 font-sans text-[13px] leading-[18px] text-popover-foreground shadow-popover"
      data-testid="toast"
      data-tone={notice.tone}
    >
      <Icon className={cn("mt-px size-4 shrink-0", className)} aria-hidden="true" />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-muted-foreground">
        <p className="font-medium text-pretty break-words text-foreground" data-testid="toast-title">
          {notice.text}
        </p>
        {notice.description ? <p className="text-pretty break-words">{notice.description}</p> : null}
        {notice.detail ? <p className="text-xs break-words">{notice.detail}</p> : null}
        {link || notice.requestId ? (
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {link ? (
              <Link
                href={link.href}
                onClick={() => toast.dismiss(id)}
                className="rounded-xs font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline"
                data-testid="toast-link"
              >
                {link.label}
              </Link>
            ) : null}
            {notice.requestId ? (
              <span className="font-mono text-xs break-all text-fg-subtle" data-testid="toast-request-id">
                {t("requestId", { id: notice.requestId })}
              </span>
            ) : null}
          </div>
        ) : null}
      </div>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        className="-my-1 shrink-0"
        onClick={() => toast.dismiss(id)}
        aria-label={t("dismiss")}
        data-testid="toast-dismiss"
      >
        <X aria-hidden="true" />
      </Button>
    </div>
  );
}

/** Shows `notice` as a toast and returns its id; a failure or a running toast stays until dismissed or replaced. */
export function notify(notice: Notice): string | number {
  const sticky = notice.tone === "error" || notice.tone === "running";
  return toast.custom((id) => <ToastCard id={id} notice={notice} />, {
    id: notice.id,
    duration: sticky ? Infinity : TOAST_MS,
  });
}

/**
 * A write that failed: what could not be done as the title, then the words `useWriteFailure` chose for the hub's
 * answer, its message and the request id. It stays until dismissed.
 */
export function notifyFailure(title: string, failure: WriteFailure, link?: ToastLink | null): string | number {
  return notify({
    tone: "error",
    text: title,
    description: failure.text,
    detail: failure.detail,
    requestId: failure.requestId,
    link: link ?? null,
  });
}

/** The page's toaster, with its landmark named in the visitor's language. */
export function HubToaster() {
  const t = useTranslations("toast");
  return <Toaster containerAriaLabel={t("region")} />;
}
