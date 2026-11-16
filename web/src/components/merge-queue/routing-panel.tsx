"use client";

import { Gauge, PencilLine } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { Tag } from "@/components/data/identifier";
import { Button } from "@/components/ui/button";
import type { AutonomyPolicy } from "@/lib/merge-queue";
import { cn } from "@/lib/utils";

import { Panel } from "./chrome";
import { LEVELS, readRouting, SCORER_OFF } from "./routing-model";

// The editor loads the first time an admin opens it, so it stays out of the policy page's first load.
const loadDialog = () => import("./routing-dialog");
const LazyRoutingDialog = dynamic(() => loadDialog().then((module) => module.RoutingDialog), { ssr: false });

function Row({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-xs leading-5 text-muted-foreground">{label}</dt>
      <dd className="-mt-1.5 min-w-0 [overflow-wrap:anywhere] sm:mt-0" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

const Model = ({ name }: { name: string | null }) => <span className={cn("font-mono text-xs", name === null && "font-sans text-fg-subtle")}>{name ?? "-"}</span>;

/**
 * The project's Token strategy (the section `routing` of its policy) as the revision shown reads it: on or off, the
 * strategy, the weekly reserve and the five-hour ceiling, the scorer, and the ladder and overflow model of each
 * difficulty. An admin edits it from the newest revision in a dialog that loads when first opened.
 */
export function RoutingPanel({ project, policy, editable }: { project: string; policy: AutonomyPolicy; editable: boolean }) {
  const t = useTranslations("policy.routing");
  const routing = readRouting(policy.effective);
  const [open, setOpen] = useState<boolean | null>(null);
  return (
    <Panel
      title={t("title")}
      action={
        <>
          <Tag className={cn(routing.enabled && "bg-brand-soft text-brand")} data-testid="routing-state">
            {routing.enabled ? t("on") : t("off")}
          </Tag>
          {editable ? (
            <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(true)} onPointerEnter={() => void loadDialog()} onFocus={() => void loadDialog()} data-testid="routing-edit">
              <PencilLine aria-hidden="true" />
              {t("edit")}
            </Button>
          ) : null}
        </>
      }
      testId="policy-routing"
    >
      <p className="flex items-start gap-2 border-b px-4 py-2.5 text-xs text-pretty text-muted-foreground">
        <Gauge className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
        {routing.enabled ? t(`strategyHints.${routing.strategy}`) : t("offText")}
      </p>
      <dl className="grid grid-cols-1 gap-x-4 gap-y-2 px-4 py-3 text-[13px] sm:grid-cols-[minmax(8rem,max-content)_minmax(0,1fr)]">
        <Row label={t("strategy")} testId="routing-value-strategy">
          {t(`strategies.${routing.strategy}`)}
        </Row>
        <Row label={t("thresholds")} testId="routing-value-thresholds">
          {t("thresholdsValue", { reserve: routing.hard_reserve_pct, ceiling: routing.five_hour_ceiling_pct })}
        </Row>
        <Row label={t("scorer")} testId="routing-value-scorer">
          {routing.scorer === SCORER_OFF ? t("scorerOff") : <Model name={routing.scorer} />}
        </Row>
      </dl>
      <div className="overflow-x-auto border-t">
        <table className="w-full text-[13px]" data-testid="routing-models">
          <caption className="sr-only">{t("modelsCaption")}</caption>
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th scope="col" className="px-4 py-2 font-normal">
                {t("difficulty")}
              </th>
              <th scope="col" className="px-2 py-2 font-normal">
                {t("ladder.title")}
              </th>
              <th scope="col" className="px-4 py-2 font-normal">
                {t("overflow.title")}
              </th>
            </tr>
          </thead>
          <tbody>
            {LEVELS.map((level) => (
              <tr key={level} className="border-t" data-level={level}>
                <th scope="row" className="px-4 py-2 text-left font-normal">
                  {t(`levels.${level}`)}
                </th>
                <td className="px-2 py-2">
                  <Model name={routing.ladder[level]} />
                </td>
                <td className="px-4 py-2">
                  <Model name={routing.overflow[level]} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {open !== null ? <LazyRoutingDialog key={String(open)} project={project} policy={policy} open={open} onOpenChange={setOpen} /> : null}
    </Panel>
  );
}
