"use client";

import { ArrowLeft, FileQuestion } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton, StatePanel } from "@/components/states/states";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { displayName } from "@/lib/kg/graph";
import { kgNodeQuery } from "@/lib/kg/queries";
import { kgHref } from "@/lib/kg/routes";
import type { Hops, NodeDetail } from "@/lib/kg/types";

import { KindShape, LabelBadge } from "./badges";
import { NeighbourhoodSection } from "./neighbourhood";
import { NodeEvidence, NodeFacts, NodeRelations } from "./node-details";

function BackLink({ project }: { project: string }) {
  const t = useTranslations("kg.node");
  return (
    <Link
      href={kgHref(project)}
      className="inline-flex w-fit items-center gap-1.5 rounded-md text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
      data-testid="kg-back"
    >
      <ArrowLeft className="size-4" aria-hidden="true" />
      {t("back")}
    </Link>
  );
}

/** A node the member cannot see: missing and hidden alike, as the API answers. */
function NodeNotFound({ project, id }: { project: string; id: string }) {
  const t = useTranslations("kg.node");
  return (
    <StatePanel
      icon={FileQuestion}
      title={t("notFoundTitle")}
      description={
        <>
          <span className="block">{t("notFoundDescription")}</span>
          {id ? <code className="mt-2 block font-mono text-xs break-all text-foreground">{id}</code> : null}
        </>
      }
      testId="state-not-found"
    >
      <Button asChild variant="outline" size="lg">
        <Link href={kgHref(project, id ? { q: id } : undefined)}>{t("searchInstead")}</Link>
      </Button>
    </StatePanel>
  );
}

function Node({ project, detail, hops, neighbourhoodError }: { project: string; detail: NodeDetail; hops: Hops; neighbourhoodError: ApiErrorInfo | null }) {
  const t = useTranslations("kg.node");
  const { node } = detail;
  return (
    <>
      <PageHeader
        eyebrow={
          <span className="inline-flex items-center gap-1.5 normal-case">
            <KindShape kind={node.kind} />
            {node.kind}
          </span>
        }
        title={<span data-testid="kg-node-title">{displayName(node)}</span>}
        description={<span className="font-mono text-xs break-all">{node.id}</span>}
        meta={
          <>
            <LabelBadge label={node.label} />
            <span className="rounded-4xl border px-2 py-0.5 font-mono text-xs" title={t("status")}>
              <span className="sr-only">{t("status")}: </span>
              {node.status}
            </span>
          </>
        }
      />
      {!detail.graph.latest ? (
        <Alert>
          <AlertDescription>{t("olderBuild", { id: detail.graph.build_id })}</AlertDescription>
        </Alert>
      ) : null}
      <NeighbourhoodSection project={project} id={node.id} hops={hops} initialError={neighbourhoodError} />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
        <div className="flex min-w-0 flex-col gap-6">
          <NodeFacts node={node} evidence={detail.evidence} />
          <NodeEvidence project={project} node={node} evidence={detail.evidence} />
        </div>
        <div className="min-w-0">
          <NodeRelations project={project} detail={detail} />
        </div>
      </div>
    </>
  );
}

/** /p/{project}/kg/node?id=&hops=: one node, its neighbourhood as a graph and a table, and all its relations. */
export function KgNodePage({
  project,
  id,
  hops,
  errors,
}: {
  project: string;
  id: string;
  hops: Hops;
  errors: { node: ApiErrorInfo | null; neighbourhood: ApiErrorInfo | null };
}) {
  const state = useHubQuery({ ...kgNodeQuery(browserApi, project, id), enabled: id !== "" }, errors.node);
  if (!id || (state.status === "error" && state.error.status === 404)) {
    return (
      <>
        <BackLink project={project} />
        <NodeNotFound project={project} id={id} />
      </>
    );
  }
  return (
    <>
      <BackLink project={project} />
      <QueryView state={state} loading={<PageSkeleton />}>
        {(detail) => <Node project={project} detail={detail} hops={hops} neighbourhoodError={errors.neighbourhood} />}
      </QueryView>
    </>
  );
}
