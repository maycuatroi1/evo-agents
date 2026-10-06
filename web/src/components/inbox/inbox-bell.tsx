"use client";

import { useQuery } from "@tanstack/react-query";
import { Bell, BellDot } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { bellNumber } from "./model";
import { INBOX_HREF, notificationCountQuery } from "./queries";

/**
 * The bell in the top bar: a link to the Inbox with the number of unread notifications, read every 10 seconds. Its
 * accessible name says the number and how many decisions wait for the visitor's answer; when the number grows, a
 * polite live region says how many arrived, so a screen reader hears of them without moving to the bell. Until the
 * first count arrives (and when it cannot be read) the bell shows no number.
 */
export function InboxBell() {
  const t = useTranslations("inbox.bell");
  const pathname = usePathname();
  const { data } = useQuery(notificationCountQuery(browserApi));
  const unread = data?.unread ?? 0;
  const decisions = data?.open_decisions ?? 0;
  const [seen, setSeen] = useState<number | null>(null);
  const [arrived, setArrived] = useState(0);
  // Compare each count with the last one seen while rendering (no effect): more unread than before is news.
  if (data && seen !== unread) {
    if (seen !== null && unread > seen) setArrived(unread - seen);
    else if (unread < (seen ?? 0)) setArrived(0);
    setSeen(unread);
  }
  const current = pathname === INBOX_HREF;
  const label = data ? t("label", { unread, decisions }) : t("labelUnknown");

  return (
    <>
      <Link
        href={INBOX_HREF}
        aria-label={label}
        aria-current={current ? "page" : undefined}
        title={label}
        className={cn(
          "relative inline-flex size-9 shrink-0 items-center justify-center rounded-lg text-muted-foreground transition-colors",
          "hover:bg-muted hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring",
          current && "bg-accent text-accent-foreground",
          unread > 0 && "text-foreground",
        )}
        data-testid="inbox-bell"
        data-unread={data ? unread : undefined}
        data-decisions={data ? decisions : undefined}
      >
        {unread > 0 ? <BellDot className="size-5" aria-hidden="true" /> : <Bell className="size-5" aria-hidden="true" />}
        {unread > 0 ? (
          <span
            className="pointer-events-none absolute -top-0.5 -right-0.5 flex h-4.5 min-w-4.5 items-center justify-center rounded-full bg-primary px-1 text-[0.6875rem] leading-none font-semibold text-primary-foreground tabular-nums ring-2 ring-background"
            aria-hidden="true"
            data-testid="inbox-bell-count"
          >
            {bellNumber(unread)}
          </span>
        ) : null}
      </Link>
      <span className="sr-only" aria-live="polite" aria-atomic="true" data-testid="inbox-bell-live">
        {arrived > 0 ? t("arrived", { count: arrived }) : ""}
      </span>
    </>
  );
}
