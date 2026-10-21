"use client";

import { Lock } from "lucide-react";
import { useTranslations } from "next-intl";

import { OutputStrip } from "@/components/data/output-strip";
import { useStatusText } from "@/components/status/status-badge";

import type { Decision } from "./queries";

/**
 * The parts of the kit's DecisionCard (decision-view.tsx, mobile-decision.tsx) that say what is not asked of the
 * visitor: why they see no form, that the decision expired or was cancelled, and the files the agent attached to its
 * question (`evo-agents worker ask --attach`, or what a local session sent since the person's last message), as a
 * strip whose chips open the viewer, the bytes served to the decision's readers.
 */

/** The files the question carries; nothing when it carries none. */
export function DecisionFiles({ decision }: { decision: Decision }) {
  const t = useTranslations("outputs.strip");
  return <OutputStrip outputs={decision.outputs ?? []} label={t("question")} testId="decision-files" />;
}

/** Why the visitor sees no form: someone else's decision, or one whose run can no longer take an answer. */
export function NoFormNote({ decision, access }: { decision: Decision; access: "notOwner" | "runGone" }) {
  const t = useTranslations("inbox.decision");
  const tState = useStatusText("run");
  return (
    <p className="flex items-start gap-2 rounded-sm bg-surface-sunken px-3 py-2.5 text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="decision-locked" data-reason={access}>
      <Lock className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>
        {access === "notOwner"
          ? t("onlyOwner", { owner: decision.owner, run: decision.run_id })
          : t("runGone", { run: decision.run_id, state: tState(decision.run_state) })}
      </span>
    </p>
  );
}

export function ClosedNote({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  if (decision.state !== "expired" && decision.state !== "cancelled") return null;
  return (
    <p className="flex items-start gap-2 text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid="decision-closed">
      <Lock className="mt-px size-4 shrink-0" aria-hidden="true" />
      <span>{decision.state === "expired" ? t("expired", { run: decision.run_id }) : t("cancelled", { run: decision.run_id })}</span>
    </p>
  );
}
