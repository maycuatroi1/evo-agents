"use client";

import { Braces, ListChecks } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { Segmented } from "@/components/data/segmented";
import { Prose, ValueView, Verbatim } from "@/components/plans/prose";
import { isObject, parsePlan } from "@/lib/plans";

/**
 * A proposal's draft plan, as the plan pages show a plan: its title, goal and steps in outcome form (what, the verify
 * command, the acceptance the step is done by), and the sections it adds; or the JSON the review run wrote, as it is.
 * Accepting a proposal turns this draft into a plan on the hub (a later step of the night shift).
 */
export function DraftPlan({ plan }: { plan: Record<string, unknown> }) {
  const t = useTranslations("curator.draft");
  const [view, setView] = useState<"plan" | "json">("plan");
  const parsed = parsePlan(isObject(plan) ? plan : {});
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="proposal-draft">
      <div className="flex flex-wrap items-center gap-2">
        {parsed.id ? <Identifier value={parsed.id} /> : null}
        <Segmented
          label={t("view")}
          value={view}
          onChange={setView}
          className="ml-auto"
          options={[
            { value: "plan", label: t("rendered"), icon: ListChecks, testId: "proposal-draft-plan" },
            { value: "json", label: t("raw"), icon: Braces, testId: "proposal-draft-json" },
          ]}
        />
      </div>
      {view === "json" ? (
        <Verbatim className="max-h-[32rem] overflow-auto text-xs" testId="proposal-draft-raw">
          {JSON.stringify(plan, null, 2)}
        </Verbatim>
      ) : (
        <div className="flex min-w-0 flex-col gap-4">
          {parsed.title ? <p className="text-sm font-medium text-pretty [overflow-wrap:anywhere]">{parsed.title}</p> : null}
          {parsed.goal ? (
            <div className="flex flex-col gap-1">
              <h4 className="text-xs font-medium text-muted-foreground">{t("goal")}</h4>
              <Prose>{parsed.goal}</Prose>
            </div>
          ) : null}
          <div className="flex flex-col gap-2">
            <h4 className="text-xs font-medium text-muted-foreground">{t("steps", { count: parsed.steps.length })}</h4>
            <ol className="flex flex-col gap-3" data-testid="proposal-draft-steps">
              {parsed.steps.map((step) => {
                const acceptance = step.extra.find(([key]) => key === "acceptance")?.[1];
                return (
                  <li key={step.key} className="flex min-w-0 flex-col gap-2 rounded-md border px-3 py-2.5">
                    <div className="flex min-w-0 flex-wrap items-center gap-2">
                      <span className="font-mono text-xs text-muted-foreground tabular-nums">{step.key}</span>
                      <span className="min-w-0 text-sm font-medium [overflow-wrap:anywhere]">{step.title ?? t("untitled")}</span>
                      {step.repo ? <Identifier value={step.repo} className="ml-auto" /> : null}
                    </div>
                    {step.what ? <Prose className="text-[13px] text-muted-foreground">{step.what}</Prose> : null}
                    {acceptance !== undefined ? (
                      <div className="flex flex-col gap-1">
                        <h5 className="text-xs font-medium text-muted-foreground">{t("acceptance")}</h5>
                        <ValueView value={acceptance} />
                      </div>
                    ) : null}
                    {step.verify ? (
                      <div className="flex flex-col gap-1">
                        <h5 className="text-xs font-medium text-muted-foreground">{t("verify")}</h5>
                        <Verbatim className="text-xs">{step.verify}</Verbatim>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </div>
          {parsed.sections
            .filter(([key]) => key !== "id")
            .map(([key, value]) => (
              <div key={key} className="flex flex-col gap-1">
                <h4 className="font-mono text-xs text-muted-foreground">{key}</h4>
                <ValueView value={value} />
              </div>
            ))}
        </div>
      )}
    </div>
  );
}
