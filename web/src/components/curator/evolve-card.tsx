"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { type Evolution, evolutionsQuery, evolutionSubject, isEvolutionState } from "./evolve-queries";
import { NUMBER, Panel, PanelSkeleton, PanelTable, TABLE_CELL, TABLE_HEAD } from "./panel";
import { TEXT_LINK } from "./parts";
import { proposalHref } from "./queries";

const STATE_TONE: Record<string, string> = {
  queued: "text-muted-foreground",
  running: "text-brand",
  done: "text-foreground",
  failed: "text-danger",
};

function EvolutionRow({ project, item }: { project: string; item: Evolution }) {
  const t = useTranslations("curator.evolve");
  return (
    <tr className="border-t align-top" data-testid="evolve-row" data-state={item.state} data-target={item.target}>
      <td className={cn(TABLE_CELL, "font-mono tabular-nums")}>#{item.id}</td>
      <td className={cn(TABLE_CELL, "min-w-48 text-foreground")}>
        <div className="flex flex-col gap-0.5 py-2">
          <span className="text-xs text-fg-subtle">{t(`target.${item.target === "agent_config" ? "agent" : "git"}`)}</span>
          <span className="font-mono text-xs [overflow-wrap:anywhere]" data-testid="evolve-subject">
            {evolutionSubject(item)}
          </span>
          {item.reason ? <span className="text-xs text-pretty text-fg-subtle [overflow-wrap:anywhere]">{item.reason}</span> : null}
        </div>
      </td>
      <td className={cn(TABLE_CELL, STATE_TONE[item.state] ?? "text-muted-foreground")} data-testid="evolve-state">
        {isEvolutionState(item.state) ? t(`state.${item.state}`) : item.state}
      </td>
      <td className={cn(TABLE_CELL, NUMBER)}>{item.iterations}</td>
      <td className={cn(TABLE_CELL, NUMBER)} data-testid="evolve-front">
        {item.frontier.length}
      </td>
      <td className={TABLE_CELL}>
        {item.proposal_id !== null ? (
          <Link href={proposalHref(project, item.proposal_id)} className={TEXT_LINK}>
            #{item.proposal_id}
          </Link>
        ) : (
          <span aria-label={t("noProposal")}>-</span>
        )}
      </td>
      <td className={cn(TABLE_CELL, "whitespace-nowrap")}>
        <When value={item.updated_at} />
      </td>
    </tr>
  );
}

/**
 * The Curator's evolutions on its page: each run that rewrote a harness's files or an agent's configuration, newest
 * first, with its state, the iterations it ran, how many candidates stand on its Pareto front, and the proposal it
 * serves. A project with no evolution shows nothing.
 */
export function EvolveCard({ project }: { project: string }) {
  const t = useTranslations("curator.evolve");
  const found = useQuery(evolutionsQuery(browserApi, project));
  let body;
  if (found.isPending) body = <PanelSkeleton rows={3} />;
  else if (found.isError) {
    body = (
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 text-[13px] text-muted-foreground" role="alert">
        <span>{t("unreadable")}</span>
        <Button variant="outline" size="sm" onClick={() => void found.refetch()}>
          {t("retry")}
        </Button>
      </div>
    );
  } else if (found.data.evolutions.length === 0) return null;
  else {
    body = (
      <PanelTable label={t("caption")} testId="evolve-table">
        <thead>
          <tr>
            <th className={TABLE_HEAD}>{t("head.id")}</th>
            <th className={TABLE_HEAD}>{t("head.subject")}</th>
            <th className={TABLE_HEAD}>{t("head.state")}</th>
            <th className={cn(TABLE_HEAD, "text-right")}>{t("head.iterations")}</th>
            <th className={cn(TABLE_HEAD, "text-right")}>{t("head.front")}</th>
            <th className={TABLE_HEAD}>{t("head.proposal")}</th>
            <th className={TABLE_HEAD}>{t("head.moved")}</th>
          </tr>
        </thead>
        <tbody>
          {found.data.evolutions.map((item) => (
            <EvolutionRow key={item.id} project={project} item={item} />
          ))}
        </tbody>
      </PanelTable>
    );
  }
  return (
    <Panel title={t("title")} sub={t("sub")} testId="evolve-card">
      {body}
    </Panel>
  );
}
