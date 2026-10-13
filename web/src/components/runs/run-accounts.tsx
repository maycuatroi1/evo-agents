"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRightLeft, ChevronRight, OctagonX } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Tag } from "@/components/data/identifier";
import { Badge } from "@/components/ui/badge";
import { browserApi } from "@/lib/api/browser";

import { type RunAccount, runAccountsQuery, type RunAccountEvent } from "./account-model";
import { isActiveState, type Run } from "./queries";

/**
 * The accounts a run took (docs/credentials.md, section "Accounts"), a card of the run page's side column: for each
 * variable, the account the run has now by name, how it came by it, how many times it failed over, and each usage
 * limit it hit, oldest first: the account that hit it, the one the run went on with or that it stopped, and until when
 * the account cools down. A link to the Accounts page in its head. Never a value: the hub's answer carries none. Shown
 * to the member who dispatched the run alone, as the API answers only them, and only once the run took an account.
 */

const ACCOUNTS_HREF = "/accounts" as Route;

function When({ at }: { at: string }) {
  const format = useFormatter();
  const date = new Date(at);
  return (
    <time dateTime={at} title={format.dateTime(date, { dateStyle: "full", timeStyle: "medium" })} className="tabular-nums">
      {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
    </time>
  );
}

const STRONG = (chunks: ReactNode) => <span className="font-mono font-medium text-foreground">{chunks}</span>;

function EventItem({ event }: { event: RunAccountEvent }) {
  const t = useTranslations("runs.detail.accounts");
  const names = { account: event.account ?? t("gone"), to: event.to ?? t("gone") };
  const Icon = event.action === "failover" ? ArrowRightLeft : OctagonX;
  return (
    <li className="flex items-start gap-2 text-xs" data-testid="run-account-event" data-action={event.action}>
      <Icon className={event.action === "stop" ? "mt-0.5 size-3.5 shrink-0 text-danger" : "mt-0.5 size-3.5 shrink-0 text-attention"} aria-hidden="true" />
      <span className="min-w-0 text-pretty text-muted-foreground [overflow-wrap:anywhere]">
        <When at={event.at} /> · {t.rich(event.action, { ...names, b: STRONG })}
        {event.cooldown_until ? <> {t.rich("cooling", { time: () => <When at={event.cooldown_until ?? ""} /> })}</> : null}
      </span>
    </li>
  );
}

function AccountItem({ taken }: { taken: RunAccount }) {
  const t = useTranslations("runs.detail.accounts");
  return (
    <li className="flex flex-col gap-2 py-3 first:pt-0 last:pb-0" data-testid="run-account-item" data-env-var={taken.env_var}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-[13px] leading-5 font-medium text-foreground [overflow-wrap:anywhere]" data-testid="run-account-name">
          {taken.account ?? t("gone")}
        </span>
        <Tag data-testid="run-account-how" data-how={taken.how}>
          {t(`how.${taken.how}`)}
        </Tag>
        <Badge variant={taken.failovers > 0 ? "warning" : "secondary"} className="ml-auto" data-testid="run-account-failovers">
          {t("failovers", { count: taken.failovers })}
        </Badge>
      </div>
      <p className="text-xs text-muted-foreground">
        <span className="font-mono">{taken.env_var}</span> · {t.rich("chosen", { time: () => <When at={taken.chosen_at} /> })}
      </p>
      {taken.events.length > 0 ? (
        <ol className="flex flex-col gap-1.5" data-testid="run-account-events">
          {taken.events.map((event, index) => (
            <EventItem key={`${event.at}-${index}`} event={event} />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

/** The run's accounts, read every 5 seconds while it is active, for its owner; nothing while it took none. */
export function RunAccounts({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.accounts");
  const id = useId();
  const active = isActiveState(run.state);
  const query = useQuery(runAccountsQuery(browserApi, run.project, run.id, active));
  const taken = query.data ?? [];
  // Most runs take no account: the card shows once the hub says this one took some, or could not say.
  if (!query.isError && taken.length === 0) return null;

  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={id} data-testid="run-accounts">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-3">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <Link
          href={ACCOUNTS_HREF}
          className="ml-auto inline-flex items-center gap-0.5 rounded-xs text-[13px] font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline max-md:py-3"
          data-testid="run-accounts-link"
        >
          {t("link")}
          <ChevronRight className="size-3.5" aria-hidden="true" />
        </Link>
      </div>
      <div className="min-w-0 px-4 py-3 text-sm">
        {query.isError ? (
          <p className="text-pretty text-danger" role="alert" data-testid="run-accounts-error">
            {t("failed")}
          </p>
        ) : (
          <ul className="flex flex-col divide-y" data-testid="run-accounts-list">
            {taken.map((item) => (
              <AccountItem key={item.env_var} taken={item} />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
