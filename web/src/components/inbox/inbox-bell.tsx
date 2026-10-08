"use client";

import { useQuery } from "@tanstack/react-query";
import { Bell } from "lucide-react";
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
 * first count arrives (and when it cannot be read) the bell shows no number. The name says the Curator's proposals that
 * wait for an answer too, when there are some.
 *
 * As in the kit, the number is an attention pill (a person is needed), raised beside the glyph rather than over it, so
 * the bell stays whole; the link widens to hold it. A ghost icon control: 32 px, 44 px with a 20 px glyph under 768 px.
 */
export function InboxBell() {
  const t = useTranslations("inbox.bell");
  const pathname = usePathname();
  const { data } = useQuery(notificationCountQuery(browserApi));
  const unread = data?.unread ?? 0;
  const decisions = data?.open_decisions ?? 0;
  const proposals = data?.open_proposals ?? 0;
  const [seen, setSeen] = useState<number | null>(null);
  const [arrived, setArrived] = useState(0);
  // Compare each count with the last one seen while rendering (no effect): more unread than before is news.
  if (data && seen !== unread) {
    if (seen !== null && unread > seen) setArrived(unread - seen);
    else if (unread < (seen ?? 0)) setArrived(0);
    setSeen(unread);
  }
  const current = pathname === INBOX_HREF;
  const label = data ? t("label", { unread, decisions, proposals }) : t("labelUnknown");

  return (
    <>
      <Link
        href={INBOX_HREF}
        aria-label={label}
        aria-current={current ? "page" : undefined}
        title={label}
        className={cn(
          "inline-flex h-8 min-w-8 shrink-0 items-center justify-center gap-0.5 rounded-sm px-2 text-muted-foreground transition-colors duration-fast ease-standard",
          "hover:bg-accent hover:text-foreground max-md:h-11 max-md:min-w-11",
          unread > 0 && "text-foreground",
          current && "bg-surface-selected text-foreground hover:bg-surface-selected",
        )}
        data-testid="inbox-bell"
        data-unread={data ? unread : undefined}
        data-decisions={data ? decisions : undefined}
      >
        <Bell className="size-4 shrink-0 max-md:size-5" aria-hidden="true" data-testid="inbox-bell-glyph" />
        {unread > 0 ? (
          <span
            className="pointer-events-none -mt-3 inline-flex h-3.75 min-w-3.75 shrink-0 items-center justify-center rounded-full bg-attention-soft px-1 text-[10px] leading-none font-semibold text-attention tabular-nums"
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
