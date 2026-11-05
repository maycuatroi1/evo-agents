"use client";

import { Waypoints } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";

import { CommandLine } from "@/components/data/command-line";
import { StatePanel } from "@/components/states/states";
import { COST_MODEL, enableCommand, estimateCost } from "@/lib/kg/entity-graph";
import { buildsHref } from "@/lib/kg/routes";

/**
 * The Entities view of a graph whose build has no entities step (the `documents` strategy and the others without
 * `steps: [entities]`): why it is empty, what reading its documents with a model would cost, and the command that
 * turns the step on. The estimate is a figure measured on MuSiQue, not a quote: the step's ceiling caps a build.
 */
export function EntitiesEmpty({ project, documents }: { project: string; documents: number | null }) {
  const t = useTranslations("kg.explorer.entities.empty");
  const format = useFormatter();
  const usd = (value: number) =>
    format.number(value, { style: "currency", currency: "USD", maximumFractionDigits: value < 1 ? 3 : 2, minimumFractionDigits: 2 });
  const cost = estimateCost(documents ?? 0);
  return (
    <StatePanel icon={Waypoints} title={t("title")} testId="kg-entities-empty" className="max-w-xl">
      <div className="flex w-full flex-col gap-4 text-left text-[13px] leading-5">
        <p className="text-center text-muted-foreground">{t("reason")}</p>
        <dl className="grid grid-cols-2 divide-x rounded-md border" data-testid="kg-entities-estimate">
          <div className="flex min-w-0 flex-col gap-0.5 px-3 py-2">
            <dt className="text-xs text-muted-foreground">{t("documents")}</dt>
            <dd className="text-[15px] font-semibold tabular-nums">{documents === null ? t("unknown") : format.number(documents)}</dd>
          </div>
          <div className="flex min-w-0 flex-col gap-0.5 px-3 py-2">
            <dt className="text-xs text-muted-foreground">{t("estimate")}</dt>
            <dd className="text-[15px] font-semibold tabular-nums">{documents === null ? t("unknown") : t("about", { cost: usd(cost.total) })}</dd>
          </div>
        </dl>
        <p className="text-xs text-muted-foreground">
          {t("basis", {
            perDocument: usd(cost.perDocument),
            model: COST_MODEL.model,
            tokensOut: format.number(COST_MODEL.tokensOut),
            ceiling: usd(COST_MODEL.ceiling),
          })}
        </p>
        <div className="flex flex-col gap-2">
          <p className="font-medium">{t("howTitle")}</p>
          <CommandLine command={enableCommand(project)} label={t("command")} testId="kg-entities-enable" />
          <p className="text-xs text-muted-foreground">
            {t.rich("howAfter", {
              link: (chunks) => (
                <Link href={buildsHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
                  {chunks}
                </Link>
              ),
            })}
          </p>
        </div>
      </div>
    </StatePanel>
  );
}
