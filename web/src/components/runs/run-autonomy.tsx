"use client";

import { ChevronRight, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { ModeMark, useAutonomySource } from "@/components/data/autonomy";
import { Badge } from "@/components/ui/badge";

import type { Run } from "./queries";
import { Card, Fact, When } from "./run-facts";

const CLASSES = ["K1", "K2", "K3", "K4", "K5", "K6", "K7", "K8", "K9", "K10", "K11", "K12"] as const;

type Snapshot = NonNullable<Run["autonomy"]>;

function ClassLine({ klass, held }: { klass: string; held: Snapshot["classes"][string] }) {
  const source = useAutonomySource();
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1" data-testid="run-autonomy-class" data-class={klass}>
      <span className="w-8 shrink-0 font-mono text-xs font-semibold">{klass}</span>
      <ModeMark mode={held.mode} />
      {held.condition ? <span className="font-mono text-xs text-muted-foreground">{held.condition}</span> : null}
      <span className="min-w-0 text-xs text-muted-foreground [overflow-wrap:anywhere]" title={held.detail}>
        {source(held.source)}
      </span>
    </li>
  );
}

/**
 * The autonomy a run acts under (plan autonomy-levels): the person it acts for, the level the hub took when it queued
 * the run and the source that set it, the classes that are not auto (all twelve behind a disclosure), and the limits
 * on where it runs. A gate that made it stricter since is the one thing flagged.
 */
export function RunAutonomy({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.autonomy");
  const source = useAutonomySource(run.principal);
  const snapshot = run.autonomy;
  if (!snapshot) return null;
  const held = CLASSES.filter((klass) => snapshot.classes[klass]);
  const bounded = held.filter((klass) => snapshot.classes[klass]?.mode !== "auto");
  return (
    <Card
      title={t("title")}
      testId="run-autonomy"
      status={
        snapshot.tightened_at ? (
          <Badge variant="warning" data-testid="run-autonomy-tightened">
            <ShieldAlert aria-hidden="true" />
            {t("tightened")}
          </Badge>
        ) : null
      }
    >
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
        <Fact label={t("principal")} testId="run-principal">
          <span className="font-mono text-xs">{run.principal ?? "-"}</span>
          {snapshot.also && snapshot.also.length > 0 ? (
            <span className="block text-xs text-muted-foreground" data-testid="run-autonomy-also">
              {t("also", { logins: snapshot.also.join(", ") })}
            </span>
          ) : null}
        </Fact>
        <Fact label={t("level")} testId="run-autonomy-level">
          {t("levelFrom", { level: snapshot.level, source: source(snapshot.level_source) })}
        </Fact>
        <Fact label={t("taken")} testId="run-autonomy-taken">
          <When at={snapshot.taken_at} />
          {snapshot.tightened_at ? (
            <span className="block text-xs text-muted-foreground">
              {t("tightenedAt")} <When at={snapshot.tightened_at} />
            </span>
          ) : null}
        </Fact>
        {snapshot.environments ? (
          <Fact label={t("environments")} testId="run-autonomy-environments">
            {snapshot.environments.length > 0 ? snapshot.environments.join(", ") : t("noneAllowed")}
          </Fact>
        ) : null}
        {snapshot.workers ? (
          <Fact label={t("workers")} testId="run-autonomy-workers">
            {snapshot.workers.length > 0 ? snapshot.workers.join(", ") : t("noneAllowed")}
          </Fact>
        ) : null}
      </dl>
      <div className="mt-3 flex min-w-0 flex-col gap-2">
        {bounded.length > 0 ? (
          <ul className="flex min-w-0 flex-col gap-1.5" aria-label={t("boundedLabel")} data-testid="run-autonomy-bounded">
            {bounded.map((klass) => {
              const one = snapshot.classes[klass];
              return one ? <ClassLine key={klass} klass={klass} held={one} /> : null;
            })}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground" data-testid="run-autonomy-all-auto">
            {t("allAuto")}
          </p>
        )}
        <details className="group min-w-0" data-testid="run-autonomy-all">
          <summary className="inline-flex cursor-pointer list-none items-center gap-1 rounded-sm text-xs font-medium text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
            <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden="true" />
            {t("showAll", { count: held.length })}
          </summary>
          <ul className="mt-2 flex min-w-0 flex-col gap-1.5">
            {held.map((klass) => {
              const one = snapshot.classes[klass];
              return one ? <ClassLine key={klass} klass={klass} held={one} /> : null;
            })}
          </ul>
        </details>
      </div>
    </Card>
  );
}
