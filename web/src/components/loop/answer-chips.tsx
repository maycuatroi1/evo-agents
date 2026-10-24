"use client";

import { useTranslations } from "next-intl";

import { Identifier } from "@/components/data/identifier";
import { problemsHref } from "@/lib/problem-links";
import { cn } from "@/lib/utils";

/** `P-3` is a problem of this project, `lumen/P-3` one of another: the project and the code the chip opens. */
export function answerTarget(project: string, answer: string): { project: string; code: string } {
  const at = answer.lastIndexOf("/");
  return at === -1 ? { project, code: answer } : { project: answer.slice(0, at), code: answer.slice(at + 1) };
}

/**
 * The problems an opportunity answers, as chips of their codes that open each problem's page, and the number of the
 * answers of other projects the reader holds no grant on, which have no code or content to show.
 */
export function AnswerChips({
  project,
  answers,
  hidden = 0,
  className,
  testId = "opportunity-answers",
}: {
  project: string;
  answers: readonly string[];
  hidden?: number;
  className?: string;
  testId?: string;
}) {
  const t = useTranslations("opportunities.answers");
  if (answers.length === 0 && hidden === 0) return null;
  return (
    <ul aria-label={t("label")} className={cn("flex min-w-0 flex-wrap items-center gap-1", className)} data-testid={testId}>
      {answers.map((answer) => {
        const target = answerTarget(project, answer);
        return (
          <li key={answer} className="flex max-w-full min-w-0 items-center">
            <Identifier
              value={answer}
              href={problemsHref(target.project, target.code)}
              linkLabel={t("link", { code: answer })}
              className="shrink-0"
            />
          </li>
        );
      })}
      {hidden > 0 ? (
        <li className="text-xs text-fg-subtle" data-testid="opportunity-hidden-answers">
          {t("hidden", { count: hidden })}
        </li>
      ) : null}
    </ul>
  );
}
