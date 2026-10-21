"use client";

import { CircleCheck, CircleDashed, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { StatusPill } from "@/components/status/status-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

import type { SrsVersion } from "./api";
import { currentRound, type Policy, type Slot } from "./model";

/**
 * The approvals a version needs and who gave them in its current round: one row per role the policy names, required
 * first, or, without a policy, one approval of a writer who wrote none of the versions under review (of any writer,
 * its author too, when the project turned `self_approval` on, which a line under the policy then says).
 */
export function ApprovalsCard({
  project,
  policy,
  slots,
  version,
  approved,
  selfApproval,
}: {
  project: string;
  policy: Policy;
  slots: Slot[];
  version: SrsVersion;
  approved: number | null;
  selfApproval: boolean;
}) {
  const t = useTranslations("srs.review");
  const tRole = useTranslations("srs.role");
  const approvals = currentRound(version).filter((review) => review.decision === "approve");
  const none = policy.required.length === 0 && policy.optional.length === 0;
  return (
    <Card size="sm" data-testid="srs-approvals">
      <CardHeader>
        <CardTitle>
          <h2>{t("approvalsTitle")}</h2>
        </CardTitle>
        <p className="text-xs text-fg-subtle">
          {none
            ? selfApproval
              ? t("policyNoneSelf")
              : t("policyNone", { since: approved ? `v${approved}` : t("firstVersion") })
            : t("policy", {
                required: policy.required.map((role) => tRole(role)).join(", ") || t("nobody"),
                optional: policy.optional.map((role) => tRole(role)).join(", ") || t("nobody"),
              })}
        </p>
        {selfApproval ? (
          <p className="text-xs text-fg-subtle" data-testid="srs-self-approval">
            {t("selfApproval", { project })}
          </p>
        ) : null}
      </CardHeader>
      <CardContent>
        {none ? (
          approvals.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">{t("noApprovals")}</p>
          ) : (
            <ul className="flex flex-col gap-1.5" role="list">
              {approvals.map((review) => (
                <li key={`${review.reviewer}-${review.created_at}`} className="flex flex-wrap items-center gap-2 text-[13px]">
                  <StatusPill look={{ tone: "success", icon: CircleCheck }} label={t("approvedBy", { login: review.reviewer })} />
                  <When value={review.created_at} />
                </li>
              ))}
            </ul>
          )
        ) : (
          <div role="region" aria-label={t("approvalsTitle")} tabIndex={0} className="overflow-x-auto rounded-md border">
            <table className="w-full border-collapse text-sm" data-testid="srs-approval-slots">
              <caption className="sr-only">{t("approvalsCaption", { version: version.number })}</caption>
              <thead>
                <tr>
                  <th scope="col" className="border-b bg-muted/50 px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                    {t("columns.role")}
                  </th>
                  <th scope="col" className="border-b bg-muted/50 px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                    {t("columns.review")}
                  </th>
                  <th scope="col" className="border-b bg-muted/50 px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                    {t("columns.note")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {slots.map((slot) => (
                  <tr key={slot.role} data-role={slot.role}>
                    <td className="border-b px-3 py-2 align-top">
                      <span className="font-medium">{tRole(slot.role)}</span>{" "}
                      <span className="text-xs text-fg-subtle">{slot.required ? t("required") : t("optional")}</span>
                    </td>
                    <td className="border-b px-3 py-2 align-top">
                      {slot.by ? (
                        <span className="inline-flex flex-wrap items-center gap-2">
                          <StatusPill look={{ tone: "success", icon: CircleCheck }} label={t("approvedBy", { login: slot.by.reviewer })} />
                          <When value={slot.by.created_at} />
                        </span>
                      ) : (
                        <StatusPill look={{ tone: "neutral", icon: CircleDashed }} label={slot.required ? t("waiting") : t("notGiven")} />
                      )}
                    </td>
                    <td className="border-b px-3 py-2 align-top text-muted-foreground [overflow-wrap:anywhere]">{slot.by?.body || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** Every review the version had, by round, the newest first. */
export function ReviewHistory({ version }: { version: SrsVersion }) {
  const t = useTranslations("srs.review");
  const tRole = useTranslations("srs.role");
  const reviews = [...version.reviews].reverse();
  return (
    <section className="flex flex-col gap-2" aria-labelledby="srs-review-history" data-testid="srs-review-history">
      <h2 id="srs-review-history" className="text-[15px] leading-[22px] font-semibold">
        {t("historyTitle")}
      </h2>
      {reviews.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">{t("noHistory")}</p>
      ) : (
        <ol className="flex flex-col divide-y rounded-md border bg-card" role="list">
          {reviews.map((review) => {
            const approve = review.decision === "approve";
            return (
              <li key={`${review.round}-${review.reviewer}-${review.created_at}`} className="flex flex-col gap-1 px-4 py-3 text-[13px]">
                <p className="flex flex-wrap items-center gap-2">
                  <StatusPill
                    look={approve ? { tone: "success", icon: CircleCheck } : { tone: "attention", icon: Undo2 }}
                    label={approve ? t("decisions.approve") : t("decisions.request_changes")}
                  />
                  <span className="font-medium">{review.reviewer}</span>
                  {review.role ? <span className="text-muted-foreground">{t("asRole", { role: tRole(review.role) })}</span> : null}
                  <span className="text-xs text-fg-subtle">{t("round", { round: review.round })}</span>
                  <span className="ml-auto text-xs text-fg-subtle">
                    <When value={review.created_at} />
                  </span>
                </p>
                {review.body ? <p className="whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{review.body}</p> : null}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
