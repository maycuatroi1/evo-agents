"use client";

import { ArrowRight, Brain, FilePlus2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { projectHref } from "@/components/shell/nav";
import { agentHref } from "@/lib/agent-queries";

import { TEXT_LINK } from "./parts";
import type { PromotionView } from "./promotion-model";

/** A memory of the project at a revision, on its page (the memories feature's route). */
function memoryAt(project: string, memoryId: number, revision: number): Route {
  return projectHref(project, `memories/${memoryId}?revision=${revision}`);
}

/**
 * What a proposal of kind memory promotes, in place of a draft plan: the agents' memories it reads (each a link to the
 * revision it read, with its agent), the shared memory it merges into or the new one it makes, the whole memory after
 * the merge as the hub will write it, and once accepted the shared memory and revision it wrote.
 */
export function Promotion({ project, promotion }: { project: string; promotion: PromotionView }) {
  const t = useTranslations("curator.promotion");
  const { sources, target, body, written } = promotion;
  const id = useId();
  return (
    <div className="flex flex-col gap-4 px-4 py-3 text-[13px]" data-testid="proposal-promotion-body">
      <section className="flex flex-col gap-1.5" aria-labelledby={`${id}-sources`}>
        <h3 id={`${id}-sources`} className="text-xs font-medium text-muted-foreground">
          {t("sources", { count: sources.length })}
        </h3>
        <ul className="flex flex-col divide-y rounded-md border" data-testid="promotion-sources">
          {sources.map((source) => (
            <li key={source.memoryId} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2" data-memory-id={source.memoryId}>
              <Brain className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              <Link href={memoryAt(project, source.memoryId, source.revision)} className={TEXT_LINK}>
                {t("memory", { id: source.memoryId, revision: source.revision })}
              </Link>
              {source.agent ? (
                <Link href={agentHref(project, source.agent)} className={`font-mono text-xs ${TEXT_LINK}`}>
                  @{source.agent}
                </Link>
              ) : null}
            </li>
          ))}
        </ul>
      </section>
      <section className="flex flex-col gap-1.5" aria-labelledby={`${id}-target`}>
        <h3 id={`${id}-target`} className="text-xs font-medium text-muted-foreground">
          {t("target")}
        </h3>
        <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1" data-testid="promotion-target" data-target={target?.kind ?? ""}>
          {target?.kind === "merge" ? (
            <>
              <ArrowRight className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              {t.rich("merge", {
                id: target.memoryId,
                revision: target.revision,
                link: (chunks) => (
                  <Link href={memoryAt(project, target.memoryId, target.revision)} className={TEXT_LINK}>
                    {chunks}
                  </Link>
                ),
              })}
            </>
          ) : target?.kind === "new" ? (
            <>
              <FilePlus2 className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
              {t.rich("new", {
                type: target.type,
                location: target.location,
                name: target.name,
                code: (chunks) => <code className="font-mono text-xs">{chunks}</code>,
              })}
            </>
          ) : (
            <span className="text-muted-foreground">{t("noTarget")}</span>
          )}
        </p>
        {written ? (
          <p data-testid="promotion-written">
            {t.rich("written", {
              id: written.memoryId,
              revision: written.revision,
              link: (chunks) => (
                <Link href={memoryAt(project, written.memoryId, written.revision)} className={TEXT_LINK} data-testid="promotion-written-link">
                  {chunks}
                </Link>
              ),
            })}
          </p>
        ) : (
          <p className="text-muted-foreground">{t("notWritten")}</p>
        )}
      </section>
      <section className="flex flex-col gap-1.5" aria-labelledby={`${id}-merged`}>
        <h3 id={`${id}-merged`} className="text-xs font-medium text-muted-foreground">
          {t("merged")}
        </h3>
        <pre
          tabIndex={0}
          aria-labelledby={`${id}-merged`}
          className="max-h-96 max-w-full overflow-auto rounded-md border bg-muted/40 px-3 py-2.5 font-mono text-xs leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]"
          data-testid="promotion-merged"
        >
          {body}
        </pre>
      </section>
    </div>
  );
}
