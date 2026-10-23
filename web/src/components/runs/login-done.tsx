"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { useTranslations } from "next-intl";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";

import { openDecisionsQuery } from "./decision-queries";
import { runKeys } from "./queries";

/** The key of the option of a login decision that says the person signed in (`browser_login.LOGIN_DONE`). */
export const LOGIN_DONE = "done";

type LoginDecision = { id: number; project: string; run_id: number; category: string; state: string };

/** The open login decision of a run: the one the Done button on its browser answers; null when it asked none. */
export function openLogin<T extends Pick<LoginDecision, "category" | "state">>(decisions: readonly T[] | undefined): T | null {
  return decisions?.find((decision) => decision.category === "login" && decision.state === "open") ?? null;
}

/**
 * Done on the live view of a run that waits for a login: one primary button, as large as a finger (44 px), disabled
 * while the answer goes. The worker saves the browser's profile to the identity when the answer reaches it.
 */
export function LoginDone({ decision }: { decision: Pick<LoginDecision, "id" | "project" | "run_id"> }) {
  const t = useTranslations("runs.detail.browser");
  const queryClient = useQueryClient();
  const answer = useMutation({
    mutationFn: async () =>
      call(
        browserApi().POST("/v1/projects/{project}/decisions/{decision_id}/answer", {
          params: { path: { project: decision.project, decision_id: decision.id } },
          body: { option: LOGIN_DONE },
          headers: await csrfHeaders(browserApi()),
        }),
      ),
    onError: (error) =>
      notify({
        tone: "error",
        text: t("doneFailed", { id: decision.run_id }),
        detail: isApiError(error) ? error.info.message : null,
        requestId: isApiError(error) ? error.info.requestId : null,
      }),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: openDecisionsQuery(browserApi, decision.project, decision.run_id).queryKey }),
        queryClient.invalidateQueries({ queryKey: runKeys.all(decision.project) }),
      ]),
  });
  return (
    <Button
      type="button"
      className="min-h-11 cursor-pointer"
      onClick={() => answer.mutate()}
      busy={answer.isPending}
      disabled={answer.isSuccess}
      data-testid="browser-done"
    >
      <Check aria-hidden="true" />
      {answer.isPending ? t("doneSending") : t("done")}
    </Button>
  );
}

/** The Done button of the run's open login decision for the browser panel's `done` slot; null when it asked none. */
export function loginDone(decisions: readonly LoginDecision[] | undefined) {
  const login = openLogin(decisions);
  return login ? <LoginDone decision={login} /> : null;
}
