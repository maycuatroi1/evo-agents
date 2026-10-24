"use client";

import { ChevronDown, ChevronUp, GitBranch } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Button } from "@/components/ui/button";

import { firstCheckouts, groupCheckouts } from "./checkouts";
import { readCheckouts } from "./model";
import type { Worker } from "./queries";
import { SectionCard } from "./section-card";

/** How many checkouts the card lists before "Show all"; a list at most a few longer is shown whole. */
const CHECKOUTS_SHOWN = 12;
const CHECKOUTS_SLACK = 4;

/**
 * The repositories the last heartbeat found, by project, in as many columns as the width allows. The directory they
 * all sit in is said once under the heading; a long list shows its first rows and "Show all".
 */
export function CheckoutList({ worker }: { worker: Worker }) {
  const t = useTranslations("workers.detail.checkouts");
  const listId = useId();
  const [expanded, setExpanded] = useState(false);
  const checkouts = readCheckouts(worker.checkouts);
  const { base, groups } = groupCheckouts(checkouts);
  const folds = checkouts.length > CHECKOUTS_SHOWN + CHECKOUTS_SLACK;
  const shown = firstCheckouts(groups, folds && !expanded ? CHECKOUTS_SHOWN : checkouts.length);
  const description = base
    ? t.rich("descriptionIn", {
        path: base,
        code: (chunks) => <code className="font-mono text-xs text-foreground">{chunks}</code>,
      })
    : t("description");
  return (
    <SectionCard title={t("title")} description={description} testId="worker-checkouts" count={checkouts.length || undefined}>
      {checkouts.length === 0 ? (
        <p className="text-sm text-muted-foreground">{worker.last_heartbeat_at ? t("none") : t("empty")}</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div id={listId} className="flex flex-col gap-4">
            {shown.map((group) => (
              <section key={group.project ?? ""} className="flex min-w-0 flex-col gap-1.5">
                {group.project ? (
                  <h3 className="flex items-baseline gap-1.5 text-xs font-medium text-muted-foreground">
                    <span className="[overflow-wrap:anywhere]">{group.project}</span>
                    <span className="font-normal text-fg-subtle tabular-nums">{group.total}</span>
                  </h3>
                ) : null}
                <ul className="grid gap-x-6 sm:grid-cols-2 2xl:grid-cols-3">
                  {group.rows.map((row) => (
                    <li
                      key={row.name}
                      className="flex min-w-0 items-start gap-2 border-b border-border/60 py-1.5"
                      data-checkout={row.name}
                      title={row.path ?? undefined}
                    >
                      <GitBranch className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="font-mono text-[13px] [overflow-wrap:anywhere]">{row.repo}</span>
                        {row.where || row.detail ? (
                          <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">
                            {[row.where, row.detail].filter(Boolean).join(", ")}
                          </span>
                        ) : null}
                      </span>
                      {row.branch ? (
                        <span className="shrink-0 font-mono text-xs text-muted-foreground">{row.branch}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
          {folds ? (
            <Button
              type="button"
              variant="ghost"
              className="w-full text-muted-foreground"
              aria-expanded={expanded}
              aria-controls={listId}
              onClick={() => setExpanded((value) => !value)}
              data-testid="worker-checkouts-toggle"
            >
              {expanded ? <ChevronUp aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
              {expanded ? t("showFewer") : t("showAll", { count: checkouts.length })}
            </Button>
          ) : null}
        </div>
      )}
    </SectionCard>
  );
}
