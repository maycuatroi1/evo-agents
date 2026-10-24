import type { Route } from "next";
import type { useTranslations } from "next-intl";

import type { Notice } from "@/components/feedback/toast";

import { isHubDecision } from "./model";
import type { Decision } from "./queries";

type Words = ReturnType<typeof useTranslations<"inbox.answer">>;

/**
 * The toast of an answer just sent: the hub did what a decision of its own chose; an option that keeps the decision
 * open left it open, and the run waits on; a parked run resumed as another run; or the run took the answer, for its
 * agent at its next turn. `runLink` is the page of a run of the decision's project.
 */
export function answerNotice(answered: Decision, t: Words, runLink: (run: number) => Route): Notice {
  if (isHubDecision(answered)) return { tone: "success", text: t("sentHubTitle"), description: t("sentHubText") };
  if (answered.state === "open") return { tone: "success", text: t("heldTitle"), description: t("heldText", { run: answered.run_id }) };
  const next = answered.answer_run_id !== null && answered.answer_run_id !== answered.run_id ? answered.answer_run_id : null;
  if (next !== null) {
    return {
      tone: "success",
      text: t("sentResumedTitle"),
      description: t("sentResumed", { run: answered.run_id, next }),
      link: { label: t("openRun", { id: next }), href: runLink(next) },
    };
  }
  return {
    tone: "success",
    text: t("sentTitle", { run: answered.run_id }),
    description: t("sentText"),
    link: { label: t("openRun", { id: answered.run_id }), href: runLink(answered.run_id) },
  };
}
