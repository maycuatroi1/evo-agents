"use client";

import { useQuery } from "@tanstack/react-query";
import { GitCompareArrows, Undo2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";

import { type CanvasClaimDetail, type CanvasRev, claimDiffQuery, undoClaim } from "./api";
import { Provenance } from "./badges";
import { SectionCard } from "./chrome";
import { FieldChanges } from "./field-diff";
import { useCanvasWrite } from "./hooks";
import { baselineHref } from "./links";
import { conflictsOf } from "./model";

/** What a rev was: an edit, an undo of rev n, a baseline's decision, the import, a proposal accepted. */
function RevKind({ rev }: { rev: CanvasRev }) {
  const t = useTranslations("canvas.history");
  if (rev.change === "undo" && rev.undo_of) return <>{t("undoOf", { rev: rev.undo_of })}</>;
  return <>{t(`change.${rev.change}`)}</>;
}

/** Two revs side by side through the hub's diff (GET .../claims/{ref}/diff): what changed from one to the other. */
function CompareRevs({ project, slug, claim }: { project: string; slug: string; claim: CanvasClaimDetail }) {
  const t = useTranslations("canvas.history");
  const ids = useId();
  const [base, setBase] = useState(Math.max(1, claim.rev - 1));
  const [head, setHead] = useState(claim.rev);
  const revs = claim.revs.map((rev) => rev.rev).sort((a, b) => a - b);
  const diff = useQuery({ ...claimDiffQuery(browserApi, project, slug, claim.code, base, head), enabled: base !== head });
  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-md border p-3" data-testid="claim-compare">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-base`}>{t("from")}</Label>
          <NativeSelect id={`${ids}-base`} value={String(base)} onChange={(event) => setBase(Number(event.target.value))} data-testid="claim-compare-base">
            {revs.map((rev) => (
              <NativeSelectOption key={rev} value={rev}>
                {t("rev", { rev })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${ids}-head`}>{t("to")}</Label>
          <NativeSelect id={`${ids}-head`} value={String(head)} onChange={(event) => setHead(Number(event.target.value))} data-testid="claim-compare-head">
            {revs.map((rev) => (
              <NativeSelectOption key={rev} value={rev}>
                {t("rev", { rev })}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
      </div>
      {base === head ? (
        <p className="text-xs text-fg-subtle">{t("sameRev")}</p>
      ) : diff.data ? (
        <FieldChanges changes={diff.data.changes} testId="claim-compare-changes" />
      ) : diff.isError ? (
        <p className="text-xs text-danger">{t("compareFailed")}</p>
      ) : (
        <p className="text-xs text-fg-subtle">{t("comparing")}</p>
      )}
    </div>
  );
}

/**
 * A claim's history, the newest rev first: what each rev changed, field by field, who wrote it (a person, an agent for
 * a member, the hub) and why. A writer undoes the current rev, which writes the rev before it again as a new rev, so an
 * undo can be undone too; a claim someone changed since the page loaded refuses it (409) and says who.
 */
export function ClaimHistory({ project, slug, claim, writer }: { project: string; slug: string; claim: CanvasClaimDetail; writer: boolean }) {
  const t = useTranslations("canvas.history");
  const { busy, refused, run } = useCanvasWrite(project, slug);
  const [comparing, setComparing] = useState(false);
  const revs = [...claim.revs].sort((a, b) => b.rev - a.rev);
  const undo = () => void run(async () => (await undoClaim(browserApi(), project, slug, claim.code, claim.rev, ""), t("undone", { claim: claim.code, rev: claim.rev })));
  const conflict = refused ? conflictsOf(refused.error)[0] : undefined;
  return (
    <SectionCard
      title={t("heading", { count: claim.revs.length })}
      headingId="claim-history-title"
      testId="claim-history"
      action={
        claim.revs.length > 1 ? (
          <Button type="button" size="sm" variant="ghost" onClick={() => setComparing((held) => !held)} aria-expanded={comparing} data-testid="claim-compare-toggle">
            <GitCompareArrows aria-hidden="true" />
            {t("compare")}
          </Button>
        ) : null
      }
    >
      {comparing ? <CompareRevs project={project} slug={slug} claim={claim} /> : null}
      {refused ? (
        <InlineError
          text={conflict ? t("undoConflict", { claim: conflict.claim, rev: conflict.rev, login: conflict.changedBy }) : refused.failure.text}
          detail={refused.failure.detail}
          requestId={refused.failure.requestId}
        />
      ) : null}
      <ol className="flex min-w-0 flex-col gap-3">
        {revs.map((rev) => (
          <li key={rev.rev} className="flex min-w-0 flex-col gap-2 border-t pt-3 first:border-t-0 first:pt-0" data-testid="claim-rev" data-rev={rev.rev} data-change={rev.change}>
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
              <span className="font-mono text-xs font-medium text-foreground tabular-nums">{t("rev", { rev: rev.rev })}</span>
              <span className="text-muted-foreground">
                <RevKind rev={rev} />
              </span>
              {rev.baseline_id !== null && rev.change === "baseline" ? <BaselineLink project={project} slug={slug} reason={rev.reason} /> : null}
              <span className="ml-auto text-xs text-fg-subtle">
                <When value={rev.created_at} />
              </span>
            </div>
            <div className="text-xs text-muted-foreground">
              <Provenance provenance={rev.provenance} />
            </div>
            {rev.reason ? <p className="text-xs text-pretty text-muted-foreground [overflow-wrap:anywhere]">{rev.reason}</p> : null}
            {rev.change === "create" ? null : <FieldChanges changes={rev.changes} testId="claim-rev-changes" />}
            {writer && rev.rev === claim.rev ? (
              <Button type="button" size="sm" variant="outline" className="w-fit" onClick={undo} busy={busy} data-testid="claim-undo">
                <Undo2 aria-hidden="true" />
                {rev.rev === 1 ? t("undoFirst") : t("undo", { rev: rev.rev })}
              </Button>
            ) : null}
          </li>
        ))}
      </ol>
    </SectionCard>
  );
}

/** The baseline whose decision wrote a rev, named by its reason ("baseline 3: CL-1 accepted"). */
function BaselineLink({ project, slug, reason }: { project: string; slug: string; reason: string }) {
  const found = /^baseline ([0-9]+):/.exec(reason);
  if (!found) return null;
  const number = Number(found[1]);
  return (
    <Link href={baselineHref(project, slug, number) as Route} className="text-xs text-brand underline underline-offset-4 transition-colors hover:text-brand-hover">
      #{number}
    </Link>
  );
}
