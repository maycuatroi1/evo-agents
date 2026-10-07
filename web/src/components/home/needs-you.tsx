"use client";

import { MessageSquare } from "lucide-react";
import type { Route } from "next";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { MouseEvent } from "react";

import { NAME_LINK, RunRef } from "@/components/data/identifier";
import { useNow } from "@/components/kg/use-now";
import { runHref } from "@/components/runs/queries";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { decisionHref, decisionsForYou, type Overview, type OverviewDecision } from "./model";
import { CARD_LINK, HomeCard, Relative, Row, useDuration } from "./parts";

// The decision sheet carries the answer form and the Markdown of the agent's context: it loads the first time a
// decision is opened, and the Answer button asks for it as soon as the pointer or focus reaches it.
const loadSheet = () => import("@/components/inbox/decision-sheet");
export const LazyDecisionSheet = dynamic(() => loadSheet().then((module) => module.DecisionSheet), { ssr: false });
const preloadSheet = () => void loadSheet();

function useWhere() {
  const t = useTranslations("home.needs");
  return (item: { project: string; plan_id: string; plan_title: string | null; step_key: string | null }) =>
    item.step_key !== null
      ? t("where", { project: item.project, plan: item.plan_title ?? item.plan_id, step: item.step_key })
      : t("wherePlan", { project: item.project, plan: item.plan_title ?? item.plan_id });
}

/** "parks in 23 h 54 min" while the run waits, "parked 3 hours ago" once it has. */
function ParkTiming({ decision }: { decision: OverviewDecision }) {
  const t = useTranslations("home.needs");
  const duration = useDuration();
  const waiting = decision.run_state === "waiting" && decision.parks_at !== null;
  const now = useNow(waiting);
  if (decision.parks_at === null) return null;
  if (decision.run_state === "parked") {
    return <span data-testid="needs-parks">{t.rich("parked", { time: () => <Relative value={decision.parks_at!} /> })}</span>;
  }
  if (!waiting || now === null) return null;
  return <span data-testid="needs-parks">{t("parksIn", { duration: duration(Math.max(0, Date.parse(decision.parks_at) - now)) })}</span>;
}

export function NeedsYou({ overview, onAnswer }: { overview: Overview; onAnswer: (decision: OverviewDecision) => void }) {
  const t = useTranslations("home.needs");
  const where = useWhere();
  const decisions = decisionsForYou(overview);
  if (decisions.length === 0) return null;
  const total = Math.max(overview.counts.waiting_on_you, decisions.length);
  const open = (decision: OverviewDecision) => (event: MouseEvent<HTMLAnchorElement>) => {
    // A plain click answers here; a click that opens a tab or a window follows the link to the Inbox.
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onAnswer(decision);
  };
  return (
    <HomeCard
      title={t("title")}
      count={{ value: total, tone: "attention", words: t("count", { count: total }) }}
      action={
        <Link href={"/inbox" as Route} className={CARD_LINK} data-testid="needs-you-inbox">
          {t("inbox")}
        </Link>
      }
      testId="needs-you"
    >
      <ul>
        {decisions.map((decision) => (
          <Row
            key={`${decision.project}/${decision.id}`}
            testId="needs-you-item"
            data={{ "decision-id": decision.id }}
            mark={<MessageSquare className="size-4 text-attention" aria-hidden="true" />}
            title={
              <Link
                href={decisionHref(decision.id)}
                onClick={open(decision)}
                onPointerEnter={preloadSheet}
                onFocus={preloadSheet}
                className={cn(NAME_LINK, "min-w-0 truncate")}
                title={decision.question}
                data-decision-link={decision.id}
              >
                {decision.question}
              </Link>
            }
            sub={
              <>
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  <RunRef id={decision.run_id} href={runHref(decision.project, decision.run_id)} className="mr-2 text-xs" />
                  {where(decision)}
                </span>
                <span>{t.rich("asked", { time: () => <Relative value={decision.asked_at} /> })}</span>
                <ParkTiming decision={decision} />
              </>
            }
            end={
              <Button
                type="button"
                size="sm"
                onClick={() => onAnswer(decision)}
                onPointerEnter={preloadSheet}
                onFocus={preloadSheet}
                aria-label={t("answerLabel", { id: decision.id })}
                data-testid="needs-you-answer"
              >
                {t("answer")}
              </Button>
            }
          />
        ))}
      </ul>
      {total > decisions.length ? (
        <p className="border-t px-4 py-2 text-xs text-fg-subtle">{t("more", { count: total - decisions.length })}</p>
      ) : null}
    </HomeCard>
  );
}
