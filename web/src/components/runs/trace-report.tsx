"use client";

import { ChevronRight, FileText, GitBranch, GitCommitHorizontal, Terminal, TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useMemo } from "react";

import { Identifier, Tag } from "@/components/data/identifier";
import { StatusBadge } from "@/components/status/status-badge";
import { cn } from "@/lib/utils";

import { branchUrl, commitUrl, type RepoWeb } from "./forge";
import { useNewTabLabel } from "./links";
import { parseStepReport, shellLines, type StepReport } from "./step-report";
import { TermBlock } from "./trace-term";

/**
 * A step's report in the Trace, in place of its text (step-report.ts reads it): the result as a state, the step, and the
 * branch and commits as names, links to the forge when the run's repo on that branch has a web page. Under them the
 * files outside the step's scope and a stash when there are any, then the decisions and the notes, as Markdown through
 * `prose` (Markdown is another feature's part, so the trace hands it in). The files and the verify are folded rows like
 * a tool call's; the verify starts open when the step failed.
 */
export type StepReportCardProps = { report: StepReport; prose: (text: string) => ReactNode; web: RepoWeb | null };

export function StepReportCard({ report, prose, web }: StepReportCardProps) {
  const t = useTranslations("runs.detail.trace.report");
  const named = useNewTabLabel();
  const branchHref = branchUrl(web, report.branch);
  const added = report.files.filter((file) => file.note?.toLowerCase() === "new").length;
  return (
    <section
      aria-label={t("label", { step: report.step })}
      className="min-w-0 overflow-hidden rounded-md border bg-card"
      data-testid="trace-report"
      data-result={report.passed ? "pass" : "fail"}
    >
      <header className="flex min-h-10 flex-wrap items-center gap-x-2 gap-y-1.5 border-b px-3 py-2">
        <StatusBadge
          kind="run"
          status={report.passed ? "done" : "failed"}
          label={report.passed ? t("pass") : t("fail")}
          data-testid="trace-report-result"
        />
        <span className="text-sm font-semibold text-foreground">{t("title", { step: report.step })}</span>
        <span className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 sm:ml-auto">
          {report.branch ? (
            <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground">
              <GitBranch className="size-3.5 shrink-0" aria-hidden="true" />
              <span className="sr-only">{t("branch")} </span>
              <Identifier
                value={report.branch}
                externalHref={branchHref}
                linkLabel={branchHref ? named(report.branch) : undefined}
                testId="trace-report-branch"
              />
            </span>
          ) : null}
          {report.commits.length ? (
            <span className="inline-flex min-w-0 flex-wrap items-center gap-1 text-muted-foreground">
              <GitCommitHorizontal className="size-3.5 shrink-0" aria-hidden="true" />
              <span className="sr-only">{t("commits")} </span>
              {report.commits.map((sha) => {
                const href = commitUrl(web, sha);
                return (
                  <Identifier key={sha} value={sha} externalHref={href} linkLabel={href ? named(sha) : undefined} testId="trace-report-commit" />
                );
              })}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground" data-testid="trace-report-no-commit">
              {report.commitsNote ?? t("noCommit")}
            </span>
          )}
        </span>
      </header>

      <div className="flex flex-col gap-4 px-3 py-3 empty:hidden">
        {report.outsideScope.length ? (
          <Part label={t("outsideScope")} icon={<TriangleAlert className="size-3.5 text-attention" aria-hidden="true" />}>
            <ul className="flex flex-col gap-1 font-mono text-xs leading-[18px] [overflow-wrap:anywhere]" data-testid="trace-report-outside">
              {report.outsideScope.map((line, index) => (
                <li key={index}>{line}</li>
              ))}
            </ul>
          </Part>
        ) : null}
        {report.stash ? (
          <Part label={t("stash")}>
            <Identifier value={report.stash} copy testId="trace-report-stash" />
          </Part>
        ) : null}
        {report.decisions.length ? (
          <Part label={t("decisions", { count: report.decisions.length })}>
            <ol className="flex flex-col gap-2" data-testid="trace-report-decisions">
              {report.decisions.map((decision, index) => (
                <li key={index} className="grid grid-cols-[1.25rem_minmax(0,1fr)] gap-x-1">
                  <span className="pt-px font-mono text-xs leading-5 text-fg-subtle tabular-nums" aria-hidden="true">
                    {index + 1}.
                  </span>
                  {prose(decision)}
                </li>
              ))}
            </ol>
          </Part>
        ) : null}
        {report.notes ? (
          <Part label={t("notes")}>
            <div data-testid="trace-report-notes">{prose(report.notes.split("\n").join("\n\n"))}</div>
          </Part>
        ) : null}
      </div>

      {report.files.length ? (
        <Fold
          icon={FileText}
          title={t("files")}
          summary={added ? t("filesCountNew", { count: report.files.length, added }) : t("filesCount", { count: report.files.length })}
          testId="trace-report-files"
        >
          <ul className="flex flex-col gap-0.5 px-3 py-2.5 font-mono text-xs leading-[18px]">
            {report.files.map((file, index) => (
              <li key={index} className="flex min-w-0 flex-wrap items-center gap-x-1.5 [overflow-wrap:anywhere]">
                <span className="min-w-0 text-foreground">{file.path}</span>
                {file.note ? <Tag>{file.note}</Tag> : null}
              </li>
            ))}
          </ul>
        </Fold>
      ) : null}
      {report.verify || report.verifyOutput ? (
        <Fold
          icon={Terminal}
          title={t("verify")}
          summary={report.verify?.command ?? null}
          mono
          open={!report.passed}
          testId="trace-report-verify"
        >
          <div className="min-w-0">
            {report.verify ? <TermBlock text={shellLines(report.verify.command)} wrap label={t("command")} testId="trace-report-command" /> : null}
            {report.verify?.effective ? (
              <TermBlock text={shellLines(report.verify.effective)} wrap label={t("effective")} testId="trace-report-effective" />
            ) : null}
            {report.verifyOutput ? <TermBlock text={report.verifyOutput} label={t("output")} testId="trace-report-output" /> : null}
          </div>
        </Fold>
      ) : null}
    </section>
  );
}

function Part({ label, icon, children }: { label: string; icon?: ReactNode; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        {icon}
        {label}
      </p>
      {children}
    </div>
  );
}

/** A folded row of the card, like a tool call's: a chevron, an icon, a title and a one-line summary. */
function Fold({
  icon: Icon,
  title,
  summary,
  mono = false,
  open = false,
  testId,
  children,
}: {
  icon: typeof FileText;
  title: string;
  summary: string | null;
  mono?: boolean;
  open?: boolean;
  testId: string;
  children: ReactNode;
}) {
  return (
    <details className="group/fold min-w-0 border-t" open={open} data-testid={testId}>
      <summary className="flex h-9 cursor-pointer list-none items-center gap-2 px-3 text-[13px] select-none group-open/fold:border-b hover:bg-accent max-md:h-11 [&::-webkit-details-marker]:hidden">
        <ChevronRight
          className="size-3.5 shrink-0 text-fg-subtle transition-transform duration-[var(--transition-duration-base)] group-open/fold:rotate-90"
          aria-hidden="true"
        />
        <Icon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="shrink-0 font-medium text-foreground">{title}</span>
        {summary ? (
          <span className={cn("min-w-0 flex-1 truncate text-muted-foreground", mono && "font-mono text-xs leading-4")} title={summary}>
            {summary}
          </span>
        ) : null}
      </summary>
      {children}
    </details>
  );
}

export type StepReportMessageProps = {
  text: string;
  /** Markdown of the words around the report, and of the whole message when it holds none after all. */
  message: (text: string) => ReactNode;
  prose: (text: string) => ReactNode;
  webOf: (branch: string | null) => RepoWeb | null;
};

/** An agent's message that holds a step's report: the words before it, the card, the words after it. */
export function StepReportMessage({ text, message, prose, webOf }: StepReportMessageProps) {
  const found = useMemo(() => parseStepReport(text), [text]);
  if (!found) return message(text);
  return (
    <>
      {found.lead ? message(found.lead) : null}
      <StepReportCard report={found.report} prose={prose} web={webOf(found.report.branch)} />
      {found.tail ? message(found.tail) : null}
    </>
  );
}
