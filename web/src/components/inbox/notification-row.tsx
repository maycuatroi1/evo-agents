"use client";

import { ArrowRight, Check, ChevronDown, Filter, Layers } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTimeZone, useTranslations } from "next-intl";
import { type ReactNode, useId, useState } from "react";

import { Ago } from "@/components/data/ago";
import { Button } from "@/components/ui/button";
import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { NoticeKindBadge } from "./badges";
import { daysBefore, dayKey, type InboxDay, type InboxEntry, opensByItself, snippet } from "./model";
import { AskedBadges, NotificationContent, NotificationIcon, NotificationLinks, useFollow } from "./notification-item";
import { type Notification, runHref } from "./queries";

/**
 * The Inbox below what waits for an answer: one row a notification, grouped by day under a heading that stays at the top
 * while its day scrolls, and the notifications one run sent that day folded into one row (`RunGroup`). A row shows the
 * title and, while unread, the first line of the body; opening it shows the rest (`NotificationContent`) and where it
 * leads. A decision or a proposal no longer open opens in its sheet, as in the cards above.
 */

/** The row's grid: the unread dot, the icon, the title and the meta at the end. */
const ROW =
  "relative flex min-w-0 items-start gap-2.5 px-3 py-2.5 transition-colors duration-fast hover:bg-accent sm:gap-3 sm:px-4 has-[[data-inbox-row]:focus-visible]:outline-2 has-[[data-inbox-row]:focus-visible]:-outline-offset-2 has-[[data-inbox-row]:focus-visible]:outline-ring";
/** The left edge of what a row shows under its title: past the dot and the icon. */
const INDENT = "pl-[42px] pr-3 sm:pl-[52px] sm:pr-4";
/** A button or link of the row that stretches over the whole row, so a click anywhere on it opens it. */
const STRETCH = "after:absolute after:inset-0 after:content-[''] focus-visible:outline-none";

function UnreadDot({ unread }: { unread: boolean }) {
  const t = useTranslations("inbox.item");
  return (
    <span className="mt-[7px] flex w-2 shrink-0 justify-center">
      {unread ? (
        <span className="size-2 rounded-full bg-brand" data-testid="notification-unread">
          <span className="sr-only">{t("unread")}</span>
        </span>
      ) : null}
    </span>
  );
}

/**
 * Mark as read at the end of a row: always there on a touch screen, and from 768 px while the pointer or focus is on its
 * row (`group/row` of a notification, `group/run` of a run's row), so the list does not repeat it on every line.
 */
const REVEAL = {
  row: "md:opacity-0 md:group-hover/row:opacity-100 md:group-focus-within/row:opacity-100",
  run: "md:opacity-0 md:group-hover/run:opacity-100 md:group-focus-within/run:opacity-100",
};

function MarkRead({
  label,
  busy,
  onClick,
  testId,
  reveal,
}: {
  label: string;
  busy: boolean;
  onClick: () => void;
  testId: string;
  reveal: keyof typeof REVEAL;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon-sm"
      className={cn("relative z-[1] -my-1 aria-disabled:opacity-60 md:focus-visible:opacity-100", REVEAL[reveal])}
      busy={busy}
      aria-label={label}
      title={label}
      onClick={() => {
        if (!busy) onClick();
      }}
      data-testid={testId}
    >
      <Check aria-hidden="true" />
    </Button>
  );
}

/** The end of a row: the project from 1024 px, the time from 640 px (under the title below it), then its controls. */
function Meta({ project, at, children }: { project: string | null; at: string; children?: ReactNode }) {
  return (
    <div className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
      {project ? <span className="hidden max-w-40 truncate font-mono lg:inline">{project}</span> : null}
      <span className="hidden text-fg-subtle sm:inline">
        <Ago value={at} never="-" />
      </span>
      {children}
    </div>
  );
}

/** The time under the title on a phone, where the end of the row keeps its room for the title. */
function PhoneTime({ at }: { at: string }) {
  return (
    <p className="text-xs leading-4 text-fg-subtle sm:hidden">
      <Ago value={at} never="-" />
    </p>
  );
}

function Chevron({ open }: { open: boolean }) {
  return (
    <ChevronDown
      className={cn("hidden size-4 shrink-0 text-fg-subtle transition-transform duration-base motion-reduce:transition-none sm:block", open && "rotate-180")}
      aria-hidden="true"
    />
  );
}

export type RowProps = {
  notification: Notification;
  /** Where the title of a decision or a proposal leads: the Inbox with it open, the filters kept. */
  href: Route | null;
  selected: boolean;
  onOpenDecision: (id: number) => void;
  onOpenProposal?: (id: number) => void;
  onRead: (id: number) => void;
  reading: boolean;
  /** A row inside a run's row, which names the project already. */
  nested?: boolean;
};

/**
 * One notification as a row. A notice opens in place (the row is one button, `aria-expanded`); one that asks something
 * of its reader opens by itself while unread (`opensByItself`). A decision or a proposal no longer open is a link to its
 * sheet, with its state as a pill.
 */
export function NotificationRow({ notification, href, selected, onOpenDecision, onOpenProposal, onRead, reading, nested = false }: RowProps) {
  const t = useTranslations("inbox.item");
  const ids = useId();
  const unread = notification.read_at === null;
  const decision = notification.kind === "decision";
  const proposal = notification.kind === "proposal" && notification.proposal_id != null;
  const asked = decision || proposal;
  const [open, setOpen] = useState(() => opensByItself(notification));
  const follow = useFollow(notification, onOpenDecision, onOpenProposal);
  const line = !open && unread && !decision ? snippet(notification.body) : null;
  const titleClass = cn("text-sm text-foreground", unread ? "font-semibold" : "font-normal");

  return (
    <li
      className={cn("group/row min-w-0", selected && "bg-surface-selected")}
      aria-current={selected ? "true" : undefined}
      data-testid="notification"
      data-id={notification.id}
      data-kind={notification.kind}
      data-notice-kind={notification.notice_kind ?? undefined}
      data-decision-id={notification.decision_id ?? undefined}
      data-decision-state={notification.decision_state ?? undefined}
      data-proposal-id={notification.proposal_id ?? undefined}
      data-proposal-state={notification.proposal_state ?? undefined}
      data-unread={unread ? "true" : "false"}
      data-open={asked ? undefined : open ? "true" : "false"}
    >
      <div className={ROW}>
        <UnreadDot unread={unread} />
        <NotificationIcon notification={notification} className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          {asked && href ? (
            <Link
              href={href}
              onClick={follow}
              className={cn(titleClass, STRETCH, "line-clamp-2 [overflow-wrap:anywhere] hover:text-brand sm:line-clamp-1")}
              data-testid="notification-open"
              data-inbox-row=""
              data-decision-link={decision ? (notification.decision_id ?? undefined) : undefined}
              data-proposal-link={proposal ? (notification.proposal_id ?? undefined) : undefined}
            >
              {notification.title}
            </Link>
          ) : (
            <button
              type="button"
              className={cn(titleClass, STRETCH, "cursor-pointer text-left")}
              aria-expanded={open}
              aria-controls={`${ids}-details`}
              onClick={() => setOpen(!open)}
              data-testid="notification-toggle"
              data-inbox-row=""
            >
              <span className={cn("[overflow-wrap:anywhere]", !open && "line-clamp-2 sm:line-clamp-1")} data-testid="notification-title">
                {notification.title}
              </span>
            </button>
          )}
          {line ? (
            <p className="truncate text-[13px] leading-[18px] text-muted-foreground" data-testid="notification-snippet">
              {line}
            </p>
          ) : null}
          {!asked && notification.notice_kind && !open ? (
            <span className="sr-only" data-testid="notice-kind">
              <NoticeKindName kind={notification.notice_kind} />
            </span>
          ) : null}
          <PhoneTime at={notification.created_at} />
        </div>
        <Meta project={nested ? null : notification.project} at={notification.created_at}>
          {asked ? <AskedBadges notification={notification} /> : null}
          {unread ? (
            <MarkRead
              label={t("markReadLabel", { title: notification.title })}
              busy={reading}
              onClick={() => onRead(notification.id)}
              testId="notification-mark-read"
              reveal="row"
            />
          ) : null}
          {asked ? null : <Chevron open={open} />}
        </Meta>
      </div>
      {open && !asked ? (
        <div id={`${ids}-details`} className={cn("flex min-w-0 flex-col items-start gap-2 pb-3", INDENT)} data-testid="notification-details">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {notification.notice_kind ? <NoticeKindBadge kind={notification.notice_kind} /> : null}
            {notification.project && !nested ? <span className="font-mono [overflow-wrap:anywhere] lg:hidden">{notification.project}</span> : null}
          </div>
          <div className="flex w-full min-w-0 flex-col gap-2 text-sm">
            <NotificationContent notification={notification} onRead={onRead} />
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <NotificationLinks notification={notification} href={href} follow={follow} onRead={onRead} />
          </div>
        </div>
      ) : null}
    </li>
  );
}

/** A notice's kind in words, for a row whose tag shows only once it is open. */
function NoticeKindName({ kind }: { kind: NonNullable<Notification["notice_kind"]> }) {
  const t = useTranslations("inbox.noticeKind");
  return <>{t(kind)}</>;
}

/**
 * The notifications one run sent on one day, as one row: "Run #707", how many and how many unread, the newest title
 * under it. Opened, it lists each of them as a row, with links to the run and to the Inbox narrowed to that run, and
 * Mark as read takes them all at once.
 */
export function RunGroup({
  entry,
  row,
  onReadMany,
  onOnlyRun,
  reading,
}: {
  entry: InboxEntry & { type: "run" };
  /** A notification of the run as a row of its own, nested under the run's. */
  row: (notification: Notification) => ReactNode;
  onReadMany: (ids: number[]) => void;
  onOnlyRun: (run: number) => void;
  reading: boolean;
}) {
  const t = useTranslations("inbox.group");
  const ids = useId();
  // A run's row opens by itself when one of its notices would: what asks something of the reader is never folded away.
  const [open, setOpen] = useState(() => entry.notifications.some(opensByItself));
  const newest = entry.notifications[0];
  const unreadIds = entry.notifications.filter((notification) => notification.read_at === null).map((notification) => notification.id);
  return (
    <li
      className="min-w-0"
      data-testid="inbox-run-group"
      data-run-id={entry.runId}
      data-project={entry.project ?? undefined}
      data-count={entry.notifications.length}
      data-unread={entry.unread}
      data-open={open ? "true" : "false"}
    >
      <div className={cn(ROW, "group/run")}>
        <UnreadDot unread={entry.unread > 0} />
        <Layers className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <button
            type="button"
            className={cn(STRETCH, "flex min-w-0 cursor-pointer flex-wrap items-baseline gap-x-2 text-left text-sm")}
            aria-expanded={open}
            aria-controls={`${ids}-items`}
            onClick={() => setOpen(!open)}
            data-testid="inbox-run-group-toggle"
            data-inbox-row=""
          >
            <span className={cn("text-foreground", entry.unread > 0 ? "font-semibold" : "font-medium")}>{t("title", { id: entry.runId })}</span>
            <span className="text-[13px] text-muted-foreground tabular-nums">{t("count", { count: entry.notifications.length })}</span>
            {entry.unread > 0 ? <span className="text-xs font-medium text-brand tabular-nums">{t("unread", { count: entry.unread })}</span> : null}
          </button>
          {open ? null : (
            <p className="truncate text-[13px] leading-[18px] text-muted-foreground" data-testid="inbox-run-group-latest">
              {newest.title}
            </p>
          )}
          <PhoneTime at={newest.created_at} />
        </div>
        <Meta project={entry.project} at={newest.created_at}>
          {unreadIds.length > 0 ? (
            <MarkRead label={t("markRead", { id: entry.runId, count: unreadIds.length })} busy={reading} onClick={() => onReadMany(unreadIds)} testId="inbox-run-group-mark-read" reveal="run" />
          ) : null}
          <Chevron open={open} />
        </Meta>
      </div>
      {open ? (
        <div id={`${ids}-items`} className="pb-1.5">
          <div className={cn("flex flex-wrap items-center gap-x-4 gap-y-1 pb-1.5", INDENT)}>
            {entry.project ? (
              <Link
                href={runHref(entry.project, entry.runId)}
                className="inline-flex min-h-7 items-center gap-1 text-[13px] font-medium text-brand underline-offset-4 hover:underline max-md:min-h-11"
                data-testid="inbox-run-group-run-link"
              >
                {t("openRun", { id: entry.runId })}
                <ArrowRight className="size-3.5" aria-hidden="true" />
              </Link>
            ) : null}
            <Button type="button" variant="link" size="sm" className="h-auto px-0 text-[13px]" onClick={() => onOnlyRun(entry.runId)} data-testid="inbox-run-group-only">
              <Filter aria-hidden="true" />
              {t("onlyRun")}
            </Button>
          </div>
          <ul className="ml-[22px] border-l sm:ml-[32px]" aria-label={t("itemsLabel", { id: entry.runId })}>
            {entry.notifications.map(row)}
          </ul>
        </div>
      ) : null}
    </li>
  );
}

/** "Today", "Yesterday", or the day's date (with its year when it is not this one), by the page's time zone. */
function DayTitle({ dayKey: key }: { dayKey: string }) {
  const t = useTranslations("inbox.days");
  const format = useFormatter();
  const timeZone = useTimeZone() ?? "UTC";
  const now = useNow(true);
  const date = new Date(`${key}T12:00:00Z`);
  if (now !== null) {
    const today = dayKey(new Date(now).toISOString(), timeZone);
    const before = daysBefore(key, today);
    if (before === 0) return <>{t("today")}</>;
    if (before === 1) return <>{t("yesterday")}</>;
    const thisYear = today.slice(0, 4) === key.slice(0, 4);
    return <>{format.dateTime(date, { weekday: "long", day: "numeric", month: "long", ...(thisYear ? {} : { year: "numeric" }), timeZone: "UTC" })}</>;
  }
  return <>{format.dateTime(date, { weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone: "UTC" })}</>;
}

/** The days of the list, each a heading that stays under the top bar while its rows scroll, then its rows in one frame. */
export function InboxDays({
  days,
  entry,
}: {
  days: InboxDay[];
  entry: (item: InboxEntry) => ReactNode;
}) {
  const t = useTranslations("inbox.days");
  return (
    <div className="flex flex-col gap-4">
      {days.map((day) => (
        <section key={day.key} aria-labelledby={`inbox-day-${day.key}`} data-testid="inbox-day" data-day={day.key} className="flex flex-col">
          <h3
            id={`inbox-day-${day.key}`}
            className="sticky top-13 z-[2] flex items-baseline gap-2 bg-background py-1.5 text-[13px] leading-[18px] font-semibold text-foreground"
          >
            <DayTitle dayKey={day.key} />
            <span className="text-xs font-normal text-fg-subtle tabular-nums">
              {t("summary", { count: day.total, unread: day.unread })}
            </span>
          </h3>
          <ul className="divide-y overflow-hidden rounded-md border bg-card">{day.entries.map(entry)}</ul>
        </section>
      ))}
    </div>
  );
}
