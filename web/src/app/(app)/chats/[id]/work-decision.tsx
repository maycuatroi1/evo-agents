"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { InlineError } from "@/components/feedback/notice";
import { DecisionCard } from "@/components/inbox/decision-view";
import { decisionQuery } from "@/components/inbox/queries";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { workKeys } from "@/lib/work";

/**
 * An open decision of the work that asks the visitor, answered in the work card (web/DESIGN.md, Work card): the inbox's
 * DecisionCard with its answer form, read from the system's project. An answer, or a 409 when someone answered first,
 * reads the card again.
 */
export function WorkDecisionAnswer({ chatId, project, id }: { chatId: number; project: string; id: number }) {
  const t = useTranslations("chats.work.card");
  const queryClient = useQueryClient();
  const query = useQuery(decisionQuery(browserApi, project, id));
  const settled = () => void queryClient.invalidateQueries({ queryKey: workKeys.card(chatId) });
  if (query.isPending) return <Skeleton className="h-28 w-full rounded-md" />;
  if (query.isError) return <InlineError text={t("decisionFailed")} detail={query.error.message} />;
  return (
    <div className="min-w-0 sm:max-w-[46rem]" data-testid="work-decision-answer" data-decision-id={id}>
      <DecisionCard decision={query.data} where="run" onAnswered={settled} onConflict={settled} />
    </div>
  );
}
