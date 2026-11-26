"use client";

import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { localAccount } from "@/components/local-run";
import { chatHref } from "@/lib/chat-keys";

import { taskBranch } from "./model";
import type { Run } from "./queries";

/** The plan and step under the head: `brand` links with a quiet underline, told apart from the line's words without
 * colour (the kit draws them bare; axe's link-in-text-block asks for more than a colour). */
const SUB_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

/** The line under the run's head: what the run is of (its plan and step, its chat, its task), as words with links. */
export function useRunSub(
  run: Run,
  hrefs: { plan: Route; step: Route | null; curator: Route; proposals: Route },
): ReactNode {
  const t = useTranslations("runs.detail");
  const plan = run.kind === "plan";
  const local = run.kind === "local";
  const authoring = run.kind === "author";
  const review = run.kind === "review";
  const title = run.title ?? (plan ? run.plan_id : t("untitled"));
  const planLink = (chunks: ReactNode) => (
    <Link href={hrefs.plan} className={SUB_LINK} data-testid="run-plan-link">
      {chunks}
    </Link>
  );

  const sub =
          local
            ? t.rich("local.sub", { machine: run.machine ?? "", account: localAccount(run), plan: run.plan_id || "none", step: run.step_key || "none", planLink })
            : run.kind === "chat"
            ? t.rich("chatSub", {
                agent: run.profile ?? run.project,
                hop: run.hop ?? 1,
                chatLink: (chunks) =>
                  run.chat_id ? (
                    <Link href={chatHref(run.chat_id, run.chat_message_id)} className={SUB_LINK} data-testid="run-chat-link">
                      {chunks}
                    </Link>
                  ) : (
                    chunks
                  ),
              })
            : run.kind === "browser"
            ? t("browserSub", { title })
            : run.kind === "task"
            ? t("taskSub", { agent: run.profile ?? run.project, branch: taskBranch(run) })
            : authoring
            ? run.plan_id
              ? t.rich("authorPlanSub", { plan: run.plan_id, title, planLink })
              : t("authorNewSub", { title })
            : review
            ? t.rich("reviewSub", {
                curatorLink: (chunks) => (
                  <Link href={hrefs.curator} className={SUB_LINK} data-testid="run-curator-link">
                    {chunks}
                  </Link>
                ),
                proposalsLink: (chunks) => (
                  <Link href={hrefs.proposals} className={SUB_LINK} data-testid="run-proposals-link">
                    {chunks}
                  </Link>
                ),
              })
            : plan || run.step_key === null
            ? // The plan once, as its link; its title after it only when it says more than the id.
              run.title && run.title !== run.plan_id
              ? t.rich("planSubTitled", { plan: run.plan_id, title: run.title, planLink })
              : t.rich("planSub", { plan: run.plan_id, planLink })
            : t.rich("sub", {
                plan: run.plan_id,
                step: run.step_key,
                title,
                planLink,
                stepLink: (chunks) => (
                  <Link
                    href={hrefs.step ?? hrefs.plan}
                    className={SUB_LINK}
                    data-testid="run-step-link"
                  >
                    {chunks}
                  </Link>
                ),
              });
  return sub;
}
