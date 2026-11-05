"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Hammer, TriangleAlert, Waypoints } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef } from "react";

import { notify } from "@/components/feedback/toast";
import { StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import { COST_MODEL, type EntitySetup, estimateCost } from "@/lib/kg/entity-graph";
import { entityKeys, entitySetupQuery } from "@/lib/kg/entity-queries";
import { buildsHref } from "@/lib/kg/routes";

/**
 * The Entities view of a graph with no entities: why it is empty, what reading its documents with a model would cost,
 * and one button (the writer role) that turns the entities step on when the strategy has none and queues a build
 * (POST /v1/kg/{project}/entities/setup). While that build is queued or running the panel says so and reads the setup
 * again every 10 s; once the build is done the graph is read again, and the view fills if it read entities. A sink the
 * project's knowledge.yaml lacks is named, with the button off: the hub cannot add it, a push of the harness can. The
 * estimate is a figure measured on MuSiQue, not a quote: the step's ceiling caps a build.
 */
export function EntitiesEmpty({ project, documents }: { project: string; documents: number | null }) {
  const t = useTranslations("kg.explorer.entities.empty");
  const format = useFormatter();
  const client = useQueryClient();
  const setup = useQuery(entitySetupQuery(browserApi, project));
  const state = setup.data ?? null;
  const building = state?.building?.id ?? null;
  const seen = useRef<number | null>(null);
  useEffect(() => {
    // The build that was in flight is done: its graph may hold entities now.
    if (seen.current !== null && building === null) void client.invalidateQueries({ queryKey: ["kg", project] });
    seen.current = building;
  }, [building, client, project]);
  const start = useMutation({
    mutationFn: async () =>
      call(
        browserApi().POST("/v1/kg/{project}/entities/setup", {
          params: { path: { project } },
          body: {},
          headers: await csrfHeaders(browserApi()),
        }),
      ),
    onSuccess: (next) => {
      client.setQueryData(entityKeys.setup(project), next);
      if (next.building) notify({ tone: "success", text: t("queued", { id: next.building.id }) });
    },
    onError: (error) =>
      notify({
        tone: "error",
        text: t("failed"),
        detail: isApiError(error) ? error.info.message : null,
        requestId: isApiError(error) ? error.info.requestId : null,
      }),
  });
  const usd = (value: number) =>
    format.number(value, { style: "currency", currency: "USD", maximumFractionDigits: value < 1 ? 3 : 2, minimumFractionDigits: 2 });
  const cost = estimateCost(documents ?? 0);
  return (
    <StatePanel icon={Waypoints} title={t("title")} testId="kg-entities-empty" className="max-w-xl">
      <div className="flex w-full flex-col gap-4 text-left text-[13px] leading-5">
        <p className="text-center text-muted-foreground" data-testid="kg-entities-reason">
          {reasonOf(state, t)}
        </p>
        <dl className="grid grid-cols-2 divide-x rounded-md border" data-testid="kg-entities-estimate">
          <div className="flex min-w-0 flex-col gap-0.5 px-3 py-2">
            <dt className="text-xs text-muted-foreground">{t("documents")}</dt>
            <dd className="text-[15px] font-semibold tabular-nums">{documents === null ? t("unknown") : format.number(documents)}</dd>
          </div>
          <div className="flex min-w-0 flex-col gap-0.5 px-3 py-2">
            <dt className="text-xs text-muted-foreground">{t("estimate")}</dt>
            <dd className="text-[15px] font-semibold tabular-nums">{documents === null ? t("unknown") : t("about", { cost: usd(cost.total) })}</dd>
          </div>
        </dl>
        <p className="text-xs text-muted-foreground">
          {t("basis", {
            perDocument: usd(cost.perDocument),
            model: COST_MODEL.model,
            tokensOut: format.number(COST_MODEL.tokensOut),
            ceiling: usd(COST_MODEL.ceiling),
          })}
        </p>
        {state?.sink_problem ? (
          <div role="alert" className="flex gap-2 rounded-md border border-attention/30 bg-attention-soft px-3 py-2 text-xs" data-testid="kg-entities-sink">
            <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-attention" />
            <div className="flex min-w-0 flex-col gap-1">
              <p className="font-medium">{t("sinkTitle", { sink: state.sink ?? "" })}</p>
              <p className="break-words text-muted-foreground">{state.sink_problem}</p>
            </div>
          </div>
        ) : null}
        <div className="flex flex-col items-center gap-2">
          {state === null || state.building ? null : state.can_change ? (
            <Button
              onClick={() => start.mutate()}
              busy={start.isPending}
              disabled={state.sink_problem !== null}
              data-testid="kg-entities-build"
            >
              <Hammer aria-hidden="true" />
              {state.step_on ? t("buildNow") : t("turnOn")}
            </Button>
          ) : (
            <p className="text-center text-xs text-muted-foreground" data-testid="kg-entities-ask">
              {t("askWriter")}
            </p>
          )}
          <p className="text-center text-xs text-muted-foreground">
            {t.rich("howAfter", {
              link: (chunks) => (
                <Link href={buildsHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
                  {chunks}
                </Link>
              ),
            })}
          </p>
        </div>
      </div>
    </StatePanel>
  );
}

function reasonOf(state: EntitySetup | null, t: ReturnType<typeof useTranslations<"kg.explorer.entities.empty">>): string {
  if (state?.building) return t("building", { id: state.building.id, status: state.building.status });
  if (state?.step_on) return t("reasonOn");
  return t("reason");
}
