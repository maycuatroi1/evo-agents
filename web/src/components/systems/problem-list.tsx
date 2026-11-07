"use client";

import { CircleAlert, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";

import type { SystemProblem } from "@/lib/systems";
import { cn } from "@/lib/utils";

/**
 * What the hub finds wrong with a node, an edge or the whole system: errors first, which a save refuses, then warnings,
 * which it keeps. The hub's own words, in English, as the CLI prints them.
 */
export function ProblemList({ problems, className }: { problems: readonly SystemProblem[]; className?: string }) {
  const t = useTranslations("systems.designer");
  if (!problems.length) return null;
  const sorted = [...problems].sort((a, b) => (a.level === b.level ? 0 : a.level === "error" ? -1 : 1));
  return (
    <ul className={cn("flex flex-col gap-1.5", className)} aria-label={t("problems")} data-testid="system-problems">
      {sorted.map((problem, index) => {
        const error = problem.level === "error";
        const Icon = error ? CircleAlert : TriangleAlert;
        return (
          <li
            key={`${problem.path}-${index}`}
            className={cn(
              "flex items-start gap-2 rounded-md border px-2.5 py-2 text-[13px] text-pretty",
              error ? "border-danger/30 bg-danger-soft text-danger" : "border-attention/30 bg-attention-soft text-attention",
            )}
            data-testid="system-problem"
            data-level={problem.level}
          >
            <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <span>
              <span className="sr-only">{error ? t("error") : t("warning")}: </span>
              {problem.message}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
