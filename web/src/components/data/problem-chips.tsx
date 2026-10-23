"use client";

import { useQuery } from "@tanstack/react-query";
import { Bandage } from "lucide-react";
import { useTranslations } from "next-intl";

import { browserApi } from "@/lib/api/browser";
import { problemsHref } from "@/lib/problem-links";
import { problemsQuery } from "@/lib/problem-queries";
import { cn } from "@/lib/utils";

import { Identifier } from "./identifier";

/**
 * The problems a plan, a goal or a claim points to (plan problem-oriented-hub), as chips of their codes that open each
 * problem's page. Once the project's problems are read, each chip names its statement in its tooltip and in its
 * accessible name ("Problem P-3: Shops lose a sale at checkout"); `detail` also writes the statement beside each chip,
 * for a list of facts; `marked` puts the Problems icon before them, where no label says what the chips are (a page's
 * head). Nothing at all for no code, and no read either.
 */
export function ProblemChips({
  project,
  codes,
  detail = false,
  marked = false,
  className,
  testId = "problem-chips",
}: {
  project: string;
  codes: readonly string[];
  detail?: boolean;
  marked?: boolean;
  className?: string;
  testId?: string;
}) {
  const t = useTranslations("problems.chips");
  const { data } = useQuery({ ...problemsQuery(browserApi, project), enabled: codes.length > 0 });
  if (codes.length === 0) return null;
  const statements = new Map((data ?? []).map((problem) => [problem.code, problem.statement]));
  return (
    <ul
      aria-label={t("label")}
      className={cn("flex min-w-0 flex-wrap items-center", detail ? "flex-col items-start gap-2" : "gap-1", className)}
      data-testid={testId}
    >
      {marked ? (
        <li aria-hidden="true" className="flex items-center">
          <Bandage className="size-3.5 text-fg-subtle" />
        </li>
      ) : null}
      {codes.map((code) => {
        const statement = statements.get(code);
        return (
          <li key={code} className="flex max-w-full min-w-0 items-baseline gap-2">
            <Identifier
              value={code}
              href={problemsHref(project, code)}
              title={statement}
              linkLabel={statement ? t("link", { code, statement }) : t("linkCode", { code })}
              className="shrink-0"
              testId="problem-chip"
              data-problem={code}
            />
            {detail && statement ? <span className="min-w-0 text-[13px] leading-[18px] text-foreground [overflow-wrap:anywhere]">{statement}</span> : null}
          </li>
        );
      })}
    </ul>
  );
}
