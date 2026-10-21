"use client";

import { useQuery } from "@tanstack/react-query";
import { CircleCheck, CircleX, ClipboardCheck, FileDiff, MessageSquareText, TriangleAlert } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { RunRef } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { projectHref } from "@/components/shell/nav";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { type SrsDocument, type SrsProblem, type SrsVersion, srsCommentsQuery } from "./api";
import { VersionBadge, VersionChip } from "./badges";
import { commentsHref, compareHref, documentHref, reviewHref } from "./links";

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

/** A version's facts: who wrote it (and the agent's run that drafted it), what it was built on, when, and why. */
export function VersionCard({ project, doc, version }: { project: string; doc: SrsDocument; version: SrsVersion }) {
  const t = useTranslations("srs.rail");
  const approved = doc.approved_version;
  return (
    <Card size="sm" data-testid="srs-version-card">
      <CardHeader className="grid-cols-[1fr_auto]">
        <CardTitle>
          <h2>{t("version", { version: version.number })}</h2>
        </CardTitle>
        <VersionBadge state={version.state} />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <dl className="flex flex-col gap-2.5">
          <Fact label={t("author")}>{version.author}</Fact>
          {version.drafted_by_run ? (
            <Fact label={t("draftedBy")}>
              <RunRef id={version.drafted_by_run} href={projectHref(project, `runs/${version.drafted_by_run}`)} label={t("openRun", { id: version.drafted_by_run })} />
            </Fact>
          ) : null}
          <Fact label={t("basedOn")}>
            {version.parent_number ? <VersionChip number={version.parent_number} href={documentHref(project, doc.slug, { v: version.parent_number })} /> : t("import")}
          </Fact>
          <Fact label={t("saved")}>
            <When value={version.created_at} />
          </Fact>
          {version.submitted_at ? (
            <Fact label={t("submitted")}>
              <When value={version.submitted_at} />
            </Fact>
          ) : null}
          <Fact label={t("summary")}>{version.summary || <span className="text-fg-subtle">{t("noSummary")}</span>}</Fact>
        </dl>
        <div className="flex flex-wrap gap-2">
          {version.state === "in_review" ? (
            <Button asChild variant="secondary" size="sm">
              <Link href={reviewHref(project, doc.slug, version.number)}>
                <ClipboardCheck aria-hidden="true" />
                {t("review", { version: version.number })}
              </Link>
            </Button>
          ) : null}
          {approved && approved !== version.number ? (
            <Button asChild variant="secondary" size="sm">
              <Link href={compareHref(project, doc.slug, Math.min(approved, version.number), Math.max(approved, version.number))}>
                <FileDiff aria-hidden="true" />
                {t("compareWith", { version: approved })}
              </Link>
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

/** What the hub's checks found in a version: errors stop it going to review, warnings only show. */
export function ChecksCard({ problems, hrefOf }: { problems: SrsProblem[]; hrefOf: (key: string) => Route }) {
  const t = useTranslations("srs.rail");
  const errors = problems.filter((problem) => problem.level === "error").length;
  return (
    <Card size="sm" data-testid="srs-checks">
      <CardHeader>
        <CardTitle>
          <h2>{t("checks")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {problems.length === 0 ? (
          <p className="flex items-center gap-2 text-[13px] text-success">
            <CircleCheck className="size-4" aria-hidden="true" />
            {t("noProblems")}
          </p>
        ) : (
          <>
            <p className="mb-2 text-xs text-muted-foreground">{t("problemCount", { errors, warnings: problems.length - errors })}</p>
            <ul className="flex flex-col gap-2" role="list">
              {problems.map((problem, index) => {
                const error = problem.level === "error";
                const Icon = error ? CircleX : TriangleAlert;
                return (
                  <li key={`${problem.check}-${index}`} className="flex items-start gap-2 text-[13px]" data-level={problem.level}>
                    <Icon className={cn("mt-0.5 size-4 shrink-0", error ? "text-danger" : "text-muted-foreground")} aria-hidden="true" />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="sr-only">{error ? t("error") : t("warning")}: </span>
                      {problem.item ? (
                        <Link href={hrefOf(problem.item)} scroll={false} className="text-foreground hover:text-brand">
                          {problem.message}
                        </Link>
                      ) : (
                        problem.message
                      )}
                    </span>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** The document's open threads, each with the item it is about. */
export function CommentsCard({ project, slug }: { project: string; slug: string }) {
  const t = useTranslations("srs.rail");
  const { data } = useQuery({ ...srsCommentsQuery(browserApi, project, slug), retry: false });
  const open = (data ?? []).filter((comment) => comment.parent_id === null && comment.resolved_by === null);
  const replies = (id: number) => (data ?? []).filter((comment) => comment.parent_id === id).length;
  return (
    <Card size="sm" data-testid="srs-comments-card">
      <CardHeader>
        <CardTitle>
          <h2>{t("comments")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {open.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">{t("noThreads")}</p>
        ) : (
          <ul className="flex flex-col gap-2" role="list">
            {open.slice(0, 5).map((comment) => (
              <li key={comment.id} className="flex items-start gap-2 text-[13px]">
                <MessageSquareText className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
                <span className="min-w-0">
                  <span className="font-mono text-xs">{comment.item_code ?? t("onDocument")}</span>
                  <span className="block truncate text-muted-foreground">{comment.body}</span>
                  <span className="text-xs text-fg-subtle">{t("replies", { count: replies(comment.id) })}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
        <Link href={commentsHref(project, slug)} className="text-[13px] text-brand hover:text-brand-hover">
          {t("allComments")}
        </Link>
      </CardContent>
    </Card>
  );
}
