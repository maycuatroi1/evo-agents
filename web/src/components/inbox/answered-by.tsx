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

/** A channel's kind as a person reads it: telegram is Telegram. */
function channelName(kind: string): string {
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

/**
 * How an answer came: words with no option from a person are a free answer, and an answer through a
 * channel of a core plugin names it (`answered_via`, null from the web or the CLI). Nothing for an option on the web.
 */
export function AnswerVia({ decision }: { decision: Decision }) {
  const t = useTranslations("inbox.decision");
  const via = decision.answered_via ? channelName(decision.answered_via) : null;
  const free = decision.answer_option === null && Boolean(decision.answer_text) && decision.answered_by !== null;
  if (!free && via === null) return null;
  const words = free ? (via !== null ? t("freeAnswerVia", { channel: via }) : t("freeAnswer")) : t("answeredVia", { channel: via ?? "" });
  return (
    <p className="text-sm text-pretty" data-testid="decision-answer-via" data-free={free ? "true" : undefined}>
      {words}
    </p>
  );
}
