"use client";

import { useMutation } from "@tanstack/react-query";
import { Gauge } from "lucide-react";
import { useTranslations } from "next-intl";

import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";

/**
 * Asks the hub for a KG optimize run of the project now (POST /v1/kg/{project}/optimize, the writer role): 201 for a
 * new run, 200 for the run in flight. The run itself goes on in the hub's job; its report comes as a notification.
 */
export function OptimizeButton({ project }: { project: string }) {
  const t = useTranslations("kg.optimize");
  const start = useMutation({
    mutationFn: async () => {
      let created = false;
      const run = await call(
        browserApi()
          .POST("/v1/kg/{project}/optimize", {
            params: { path: { project } },
            headers: await csrfHeaders(browserApi()),
          })
          .then((result) => {
            created = result.response.status === 201;
            return result;
          }),
      );
      return { run, created };
    },
    onSuccess: ({ run, created }) =>
      created
        ? notify({ tone: "success", text: t("started", { id: run.id }) })
        : notify({ tone: "info", text: t("inFlight", { id: run.id, state: run.state }) }),
    onError: (error) =>
      notify({
        tone: "error",
        text: t("failed"),
        detail: isApiError(error) ? error.info.message : null,
        requestId: isApiError(error) ? error.info.requestId : null,
      }),
  });
  return (
    <Button
      variant="outline"
      size="sm"
      onClick={() => start.mutate()}
      disabled={start.isPending}
      data-testid="kg-optimize"
    >
      <Gauge aria-hidden="true" />
      {t("label")}
    </Button>
  );
}
