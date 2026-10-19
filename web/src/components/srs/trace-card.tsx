"use client";

import { useQueries, useQuery } from "@tanstack/react-query";
import { FlaskConical, GitCommitHorizontal, ListChecks, Network, Ticket } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Identifier, NAME_LINK, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { projectHref } from "@/components/shell/nav";
import { StatusBadge, type StepStatus } from "@/components/status/status-badge";
import { ListSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { kgGraphQuery, kgNeighbourhoodQuery, kgNodeQuery } from "@/lib/kg/queries";
import { kgHref, nodeHref } from "@/lib/kg/routes";

import type { SrsItem } from "./api";
import { kgNodeId } from "./model";
import { planStepOf, shortChange, type TraceRef, traceOf } from "./trace-model";

const STEPS_READ = 8; // plan steps whose own edges are read for their commits
const STEP_STATES: ReadonlySet<string> = new Set<StepStatus>(["pending", "in_progress", "blocked", "done"]);

function Group({ icon: Icon, title, empty, children, count }: { icon: typeof Ticket; title: string; empty: string; children: ReactNode; count: number }) {
  return (
    <div className="flex flex-col gap-2">
      <h3 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Icon className="size-3.5" aria-hidden="true" />
        {title}
      </h3>
      {count === 0 ? (
        <p className="text-[13px] text-fg-subtle">{empty}</p>
      ) : (
        <ul className="flex flex-col gap-1.5" role="list" aria-label={title}>
          {children}
        </ul>
      )}
    </div>
  );
}

function StepLine({ project, step }: { project: string; step: TraceRef }) {
  const place = planStepOf(step.id);
  const href = place
    ? projectHref(project, `plans/${encodeURIComponent(place.plan)}/steps/${encodeURIComponent(place.step)}`)
    : nodeHref(project, step.id);
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]" data-node={step.id}>
      <Link href={href} className={`${NAME_LINK} min-w-0 [overflow-wrap:anywhere]`}>
        {place ? `${place.plan}#${place.step}` : step.id}
      </Link>
      <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{step.name}</span>
      {step.status && STEP_STATES.has(step.status) ? <StatusBadge kind="step" status={step.status as StepStatus} /> : null}
    </li>
  );
}

function TraceBody({ project, focus }: { project: string; focus: string }) {
  const t = useTranslations("srs.trace");
  const hood = useQuery({ ...kgNeighbourhoodQuery(browserApi, project, focus, 2), retry: false });
  const first = hood.data ? traceOf(hood.data) : null;
  const details = useQueries({
    queries: (first?.steps ?? []).slice(0, STEPS_READ).map((step) => ({ ...kgNodeQuery(browserApi, project, step.id), retry: false })),
  });
  if (hood.isPending) return <ListSkeleton rows={3} />;
  if (hood.isError) {
    if (isApiError(hood.error) && hood.error.status === 404) {
      return <p className="text-[13px] text-muted-foreground" data-testid="srs-trace-missing">{t("missing")}</p>;
    }
    return (
      <div className="flex flex-col items-start gap-2 text-[13px] text-danger" role="alert">
        {t("failed")}
        <Button type="button" size="sm" variant="secondary" onClick={() => void hood.refetch()}>
          {t("retry")}
        </Button>
      </div>
    );
  }
  const trace = traceOf(hood.data, details.map((detail) => detail.data));
  return (
    <div className="flex flex-col gap-4" data-testid="srs-trace-groups">
      <Group icon={Ticket} title={t("tasks")} empty={t("noTasks")} count={trace.tickets.length}>
        {trace.tickets.map((ticket) => (
          <li key={ticket.id} className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]" data-node={ticket.id}>
            <Link href={nodeHref(project, ticket.id)} className={`${NAME_LINK} min-w-0 [overflow-wrap:anywhere]`}>
              {ticket.name ?? ticket.id}
            </Link>
            {ticket.status ? <Tag>{ticket.status}</Tag> : null}
          </li>
        ))}
      </Group>
      <Group icon={ListChecks} title={t("steps")} empty={t("noSteps")} count={trace.steps.length}>
        {trace.steps.map((step) => (
          <StepLine key={step.id} project={project} step={step} />
        ))}
      </Group>
      <Group icon={GitCommitHorizontal} title={t("changes")} empty={t("noChanges")} count={trace.changes.length}>
        {trace.changes.map((change) => (
          <li key={change.id} className="flex min-w-0 items-center gap-2 text-[13px]" data-node={change.id}>
            <Identifier
              value={change.id}
              href={change.uri ? undefined : nodeHref(project, change.id)}
              externalHref={change.uri}
              linkLabel={change.uri ? t("opensForge", { name: shortChange(change) }) : undefined}
            >
              {shortChange(change)}
            </Identifier>
          </li>
        ))}
      </Group>
      <Group icon={FlaskConical} title={t("tests")} empty={t("noTests")} count={trace.tests.length}>
        {trace.tests.map((test) => (
          <li key={test.id} className="min-w-0 text-[13px]" data-node={test.id}>
            <Link href={nodeHref(project, test.id)} className={`${NAME_LINK} [overflow-wrap:anywhere]`}>
              {test.name ?? test.id}
            </Link>
          </li>
        ))}
      </Group>
    </div>
  );
}

/**
 * The trace of a use case or a requirement as the project's knowledge graph holds it (plan srs-tasks-plugins, step 4):
 * its tasks, their plan steps, the commits and pull requests those name, and its tests. The graph reads approved
 * versions only, at its next build; the card says which build it read, and so when it has none or lacks the item.
 */
export function TraceCard({ project, item }: { project: string; item: SrsItem }) {
  const t = useTranslations("srs.trace");
  const focus = kgNodeId(item);
  const graph = useQuery({ ...kgGraphQuery(browserApi, project), retry: false });
  const built = graph.data?.graph ?? null;
  let body: ReactNode;
  if (!focus) body = <p className="text-[13px] text-muted-foreground">{t("noNode")}</p>;
  else if (graph.isPending) body = <ListSkeleton rows={3} />;
  else if (graph.isError || !built) {
    body = (
      <p className="text-[13px] text-muted-foreground" data-testid="srs-trace-no-graph">
        {graph.isError ? t("graphFailed") : t("noGraph")}{" "}
        <Link href={kgHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
          {t("openGraph")}
        </Link>
      </p>
    );
  } else body = <TraceBody project={project} focus={focus} />;
  return (
    <Card size="sm" data-testid="srs-trace">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        {built && focus ? (
          <p className="text-xs text-fg-subtle">
            {t("fromBuild", { id: built.build_id })} <When value={built.finished_at} />
          </p>
        ) : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {body}
        {built && focus ? (
          <Link href={nodeHref(project, focus)} className="inline-flex items-center gap-1.5 text-[13px] text-brand hover:text-brand-hover" data-testid="srs-trace-node">
            <Network className="size-4" aria-hidden="true" />
            {t("openNode")}
          </Link>
        ) : null}
      </CardContent>
    </Card>
  );
}
