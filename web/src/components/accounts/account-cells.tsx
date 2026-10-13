"use client";

import { CalendarClock, CircleCheck, CirclePause, Hourglass, Snowflake } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";

import { renewalDue } from "./model";
import type { Account, AccountState } from "./queries";

/** The cells of the accounts table that read more than one field: its state, with a token's end. */

const STATES: Record<AccountState, { icon: typeof CircleCheck; variant: "success" | "warning" | "secondary" | "destructive" }> = {
  ready: { icon: CircleCheck, variant: "success" },
  cooling: { icon: Snowflake, variant: "warning" },
  paused: { icon: CirclePause, variant: "secondary" },
  expired: { icon: Hourglass, variant: "destructive" },
};

/** A time in the visitor's short format, the full one as its tooltip. */
export function At({ value }: { value: string }) {
  const format = useFormatter();
  const date = new Date(value);
  return (
    <time dateTime={value} title={format.dateTime(date, { dateStyle: "medium", timeStyle: "short" })} className="whitespace-nowrap tabular-nums">
      {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
    </time>
  );
}

/** A day in the visitor's medium format, such as "Nov 3, 2026"; the day of the hub's time in UTC, as it was picked. */
export function Day({ value }: { value: string }) {
  const format = useFormatter();
  return (
    <time dateTime={value} className="whitespace-nowrap tabular-nums">
      {format.dateTime(new Date(value), { dateStyle: "medium", timeZone: "UTC" })}
    </time>
  );
}

/**
 * Ready, cooling until when, paused or expired, as the hub read it when it answered; under it, the day a token that
 * ends within 30 days ends, with the reminder to renew it, or the day an expired one ended.
 */
export function StateCell({ account, now }: { account: Account; now: number }) {
  const t = useTranslations("accounts");
  const { icon: Icon, variant } = STATES[account.state];
  const due = account.state === "expired" ? null : renewalDue(account, now);
  return (
    <span className="inline-flex flex-col items-start gap-0.5">
      <Badge variant={variant} data-testid="account-state" data-state={account.state}>
        <Icon aria-hidden="true" />
        {t(`state.${account.state}`)}
      </Badge>
      {account.state === "cooling" && account.cooldown_until ? (
        <span className="text-xs text-fg-subtle" title={account.cooldown_reason ?? undefined} data-testid="account-cooling-until">
          {t.rich("until", { time: () => <At value={account.cooldown_until ?? ""} /> })}
        </span>
      ) : null}
      {due ? (
        <span
          className="inline-flex items-center gap-1 text-xs font-medium text-attention"
          title={t("renewTitle", { days: due.days })}
          data-testid="account-renew-reminder"
        >
          <CalendarClock className="size-3.5 shrink-0" aria-hidden="true" />
          <span>{t.rich("renewBy", { day: () => <Day value={due.at} /> })}</span>
        </span>
      ) : null}
      {account.state === "expired" && account.expires_at ? (
        <span className="text-xs text-fg-subtle" data-testid="account-ended">
          {t.rich("ended", { day: () => <Day value={account.expires_at ?? ""} /> })}
        </span>
      ) : null}
    </span>
  );
}
