"use client";

import { Info } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { ListSkeleton } from "@/components/states/states";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect } from "@/components/ui/native-select";

import type { ApproveLock } from "./findings-model";
import type { ReviewRole, SrsUseCase } from "./api";
import { LockNote } from "./review-form";
import { holdsVersion, summarize } from "./use-case-model";
import { UseCaseRow } from "./use-case-row";

/** What the viewer can do on the use cases: review them (as which roles), wait for the config, or why they cannot. */
export type CaseAccess = { kind: "review"; login: string; roles: ReviewRole[]; lock: ApproveLock | null } | { kind: "waiting" } | { kind: "blocked"; reason: string } | { kind: "closed" };

/**
 * The use cases a version in review needs reviewed (/p/{project}/x/srs/documents/{slug}/versions/{number}): one row each
 * with what the version does to it, where it stands and the reviews that count, and the viewer's Approve and Request
 * changes. The version is approved by the review that settles the last of them, so the card says what approves it, and
 * that a use case asked for changes holds the version.
 */
export function UseCaseReviews({
  project,
  slug,
  version,
  round,
  approved,
  useCases,
  access,
}: {
  project: string;
  slug: string;
  version: number;
  round: number;
  approved: number | null;
  useCases: readonly SrsUseCase[];
  access: CaseAccess;
}) {
  const t = useTranslations("srs.review.cases");
  const tRole = useTranslations("srs.role");
  const ids = useId();
  const [picked, setPicked] = useState<ReviewRole | "">("");
  const sum = summarize(useCases);
  const holds = holdsVersion(useCases);
  const reviewing = access.kind === "review";
  const lockId = `${ids}-lock`;
  const role = reviewing && access.roles.includes(picked as ReviewRole) ? picked : "";
  return (
    <Card size="sm" data-testid="srs-cases">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        {sum.total > 0 ? (
          <p className="text-xs text-fg-subtle" aria-live="polite" data-testid="srs-cases-summary">
            {sum.asked > 0 ? t("summaryAsked", { approved: sum.approved, total: sum.total, asked: sum.asked }) : t("summary", { approved: sum.approved, total: sum.total })}
          </p>
        ) : null}
        <p className="text-xs text-fg-subtle">{sum.total > 0 ? t("auto", { version }) : t("empty", { version })}</p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {holds ? (
          <Banner icon={Info} tone="attention" title={t("holdsTitle", { version })} testId="srs-cases-holds">
            {t("holdsText")}
          </Banner>
        ) : null}
        {access.kind === "blocked" ? <p className="text-[13px] text-muted-foreground" data-testid="srs-cases-blocked">{access.reason}</p> : null}
        {access.kind === "waiting" && sum.total > 0 ? <ListSkeleton rows={1} /> : null}
        {reviewing && access.lock && sum.total > 0 ? <LockNote id={lockId} project={project} round={round} lock={access.lock} /> : null}
        {reviewing && access.roles.length > 1 && sum.total > 0 ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-role`}>{t("roleLabel")}</Label>
            <NativeSelect id={`${ids}-role`} value={role} onChange={(event) => setPicked(event.target.value as ReviewRole | "")} data-testid="srs-cases-role">
              <option value="">{t("roleAuto")}</option>
              {access.roles.map((held) => (
                <option key={held} value={held}>
                  {tRole(held)}
                </option>
              ))}
            </NativeSelect>
          </div>
        ) : null}
        {sum.total > 0 ? (
          <ul className="flex flex-col divide-y" aria-label={t("listLabel", { version })} role="list">
            {useCases.map((useCase) => (
              <UseCaseRow
                key={useCase.key}
                project={project}
                slug={slug}
                version={version}
                approved={approved}
                useCase={useCase}
                actions={reviewing ? { login: access.login, role, locked: access.lock !== null, lockId } : null}
              />
            ))}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}
