"use client";

import { CircleCheck, CircleX, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";

import type { ItemKind, SrsPreview, SrsProblem } from "./api";
import { KIND_ICONS } from "./badges";

/** The checks whose errors refuse saving, not only submitting (evo_agents.core_plugins.srs.doc.SAVE_CHECKS). */
const SAVE_CHECKS = new Set(["code_pattern", "code_duplicate", "code_taken", "code_changed", "coded_item_dropped"]);

/** The outline's indent by heading level; a table row sits deepest. */
const INDENT = ["pl-3", "pl-6", "pl-9", "pl-12", "pl-15", "pl-18", "pl-21"];

export function refusesSave(problem: SrsProblem): boolean {
  return problem.level === "error" && SAVE_CHECKS.has(problem.check);
}

function Names({ title, names, testId }: { title: string; names: string[]; testId: string }) {
  if (names.length === 0) return null;
  return (
    <div className="flex flex-col gap-1" data-testid={testId}>
      <h3 className="text-xs font-medium text-muted-foreground">{title}</h3>
      <p className="text-[13px] [overflow-wrap:anywhere]">{names.join(", ")}</p>
    </div>
  );
}

/**
 * What an import holds and would do, as the hub's dry run says: items per kind, the problems (an error in a code
 * refuses saving, any other error only submitting for review, a warning nothing), for a next version the items it adds,
 * changes and drops, and the outline of what it read with the line each item starts on.
 */
export function ImportPreview({ preview, next }: { preview: SrsPreview; next: boolean }) {
  const t = useTranslations("srs.import");
  const tKind = useTranslations("srs.itemKind");
  const refusing = preview.problems.filter(refusesSave).length;
  return (
    <Card size="sm" data-testid="srs-import-preview">
      <CardHeader>
        <CardTitle>
          <h2>{t("previewTitle")}</h2>
        </CardTitle>
        <p className={cn("flex items-center gap-1.5 text-xs", refusing ? "text-danger" : "text-success")} role="status" data-testid="srs-import-verdict">
          {refusing ? <CircleX className="size-3.5" aria-hidden="true" /> : <CircleCheck className="size-3.5" aria-hidden="true" />}
          {refusing ? t("refused", { count: refusing }) : t("ready")}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-1.5" data-testid="srs-import-counts">
          {Object.entries(preview.counts).map(([kind, count]) => {
            const Icon = KIND_ICONS[kind as ItemKind];
            return (
              <Tag key={kind} data-kind={kind}>
                {Icon ? <Icon aria-hidden="true" /> : null}
                {t("count", { kind: tKind(kind as ItemKind), count })}
              </Tag>
            );
          })}
        </div>
        {preview.problems.length ? (
          <div className="flex flex-col gap-2">
            <h3 className="text-xs font-medium text-muted-foreground">{t("problems", { count: preview.problems.length })}</h3>
            <ul className="flex flex-col gap-1.5" role="list" data-testid="srs-import-problems">
              {preview.problems.map((problem, index) => {
                const error = problem.level === "error";
                const Icon = error ? CircleX : TriangleAlert;
                return (
                  <li key={`${problem.check}-${index}`} className="flex items-start gap-2 text-[13px]" data-level={problem.level} data-refuses={refusesSave(problem) || undefined}>
                    <Icon className={cn("mt-0.5 size-4 shrink-0", error ? "text-danger" : "text-muted-foreground")} aria-hidden="true" />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="sr-only">{error ? t("error") : t("warning")}: </span>
                      {problem.line ? <span className="mr-1.5 font-mono text-xs text-fg-subtle">{t("line", { line: problem.line })}</span> : null}
                      {problem.message}
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : (
          <p className="text-[13px] text-success">{t("noProblems")}</p>
        )}
        {next ? (
          <div className="flex flex-col gap-3">
            <Names title={t("added")} names={preview.added} testId="srs-import-added" />
            <Names title={t("changed")} names={preview.changed} testId="srs-import-changed" />
            <Names title={t("removed")} names={preview.removed} testId="srs-import-removed" />
            {preview.added.length + preview.changed.length + preview.removed.length === 0 ? (
              <p className="text-[13px] text-muted-foreground">{t("sameAsLatest")}</p>
            ) : null}
          </div>
        ) : null}
        <div className="flex flex-col gap-2">
          <h3 className="text-xs font-medium text-muted-foreground">{t("outline")}</h3>
          <div role="region" aria-label={t("outline")} tabIndex={0} className="max-h-80 overflow-y-auto rounded-md border">
            <ol className="flex flex-col divide-y text-[13px]" data-testid="srs-import-outline">
              {preview.items.map((item, index) => {
                const depth = item.level === null ? 6 : Math.min(item.level - 1, 5);
                return (
                  <li key={index} className={cn("flex min-w-0 items-baseline gap-2 py-1.5 pr-3", INDENT[depth])}>
                    {item.code ? <span className="shrink-0 font-mono text-xs">{item.code}</span> : null}
                    <span className="min-w-0 [overflow-wrap:anywhere]">{item.title}</span>
                    <span className="ml-auto shrink-0 text-xs text-fg-subtle">{tKind(item.kind)}</span>
                  </li>
                );
              })}
            </ol>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
