"use client";

import { useTranslations } from "next-intl";

import type { Decision } from "./queries";

/** Who answered: the person, or the hub for a wait whose time or event came (it has no person). */
export function AnsweredBy({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  if (decision.answered_by === null && decision.category === "wait") return <span data-testid="decision-woke">{t("woke")}</span>;
  return t.rich("answeredBy", {
    login: decision.answered_by ?? decision.owner,
    who: (chunks) => <span className="font-mono text-foreground">{chunks}</span>,
  });
}
