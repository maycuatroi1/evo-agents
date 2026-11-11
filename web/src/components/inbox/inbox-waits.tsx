"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { MouseEvent } from "react";

import { Ago } from "@/components/data/ago";
import { WaitPill } from "@/components/status/wait-pill";
import { browserApi } from "@/lib/api/browser";
import type { components } from "@/lib/api/schema";
import { overviewQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { isWait, waitSubject } from "@/lib/waits";

import { decisionQuery } from "./queries";

type OverviewDecision = components["schemas"]["OverviewDecision"];

/** How often the group reads the waits again: as often as the Inbox reads its list. */
const WAITS_REFRESH_MS = 10_000;
/** The waits whose `waiting_on` the group reads to name what each waits for; the rest show the agent's reason. */
const DETAILED = 10;

/**
 * The Inbox's group of waits (web/DESIGN.md, The wait panel): the visitor's runs that wait on something else, which
 * need no answer. A wait notifies nobody, so the notifications do not list it; the group reads the open decisions of
 * the visitor's overview instead, those of category `wait` asked of them, and names what each waits for in a neutral
 * pill. A row opens the wait's panel in the Inbox's sheet, where its owner may still say Go on now or Stop waiting.
 */
export function InboxWaits({ href, selected, onOpen }: { href: (id: number) => Route; selected: number | null; onOpen: (id: number) => void }) {
  const t = useTranslations("wait.inbox");
  const overview = useQuery({ ...overviewQuery(browserApi), refetchInterval: WAITS_REFRESH_MS });
  const waits = (overview.data?.open_decisions ?? []).filter((decision) => decision.yours && isWait(decision));
  const details = useQueries({
    queries: waits.slice(0, DETAILED).map((decision) => ({ ...decisionQuery(browserApi, decision.project, decision.id), refetchInterval: WAITS_REFRESH_MS })),
  });
  if (waits.length === 0) return null;
  const follow = (id: number) => (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onOpen(id);
  };
  return (
    <section aria-labelledby="inbox-waits-title" className="flex flex-col gap-2.5" data-testid="inbox-waits">
      <h2 id="inbox-waits-title" className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
        {t("title")}
        <span className="rounded-full bg-muted px-1.5 text-xs text-muted-foreground tabular-nums" aria-hidden="true">
          {waits.length}
        </span>
        <span className="sr-only">{` (${t("count", { count: waits.length })})`}</span>
      </h2>
      <ul className="flex flex-col gap-2">
        {waits.map((decision, index) => (
          <WaitRow
            key={decision.id}
            decision={decision}
            detail={index < DETAILED ? (details[index]?.data ?? null) : null}
            href={href(decision.id)}
            selected={selected === decision.id}
            onClick={follow(decision.id)}
          />
        ))}
      </ul>
    </section>
  );
}

function WaitRow({
  decision,
  detail,
  href,
  selected,
  onClick,
}: {
  decision: OverviewDecision;
  detail: components["schemas"]["Decision"] | null;
  href: Route;
  selected: boolean;
  onClick: (event: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const t = useTranslations("wait.inbox");
  const subject = detail?.waiting_on ? waitSubject(detail.waiting_on, detail.wake_at ?? null) : { kind: "reason" as const, reason: decision.question };
  return (
    <li>
      <Link
        href={href}
        onClick={onClick}
        className={cn(
          "flex min-h-11 min-w-0 items-center gap-3 rounded-md border bg-card px-3.5 py-2.5 transition-colors hover:bg-accent sm:px-4",
          selected && "border-brand/50 bg-surface-selected ring-1 ring-brand/30",
        )}
        aria-current={selected ? "true" : undefined}
        data-decision-link={decision.id}
        data-testid="inbox-wait"
      >
        <span className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
            <span className="font-medium tabular-nums">{t("run", { run: decision.run_id })}</span>
            <WaitPill subject={subject} className="max-w-full" />
          </span>
          <span className="line-clamp-2 text-[13px] leading-[18px] text-muted-foreground [overflow-wrap:anywhere]">{decision.question}</span>
          <span className="text-xs text-muted-foreground">
            <span className="font-mono">{decision.project}</span>
            {", "}
            {t.rich("asked", { time: () => <Ago value={decision.asked_at} never="-" /> })}
          </span>
        </span>
        <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      </Link>
    </li>
  );
}
