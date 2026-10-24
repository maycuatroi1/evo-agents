"use client";

import { ArrowLeft, FileQuestion, Telescope } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK, Tag } from "@/components/data/identifier";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton, StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { filterMembers } from "@/lib/kg/map";
import { kgClusterQuery } from "@/lib/kg/queries";
import { clusterHref, explorerHref, nodeHref } from "@/lib/kg/routes";
import type { ClusterDetail, MapNode } from "@/lib/kg/types";

import { KindBadge } from "./badges";
import { ClusterDecisions } from "./decisions";
import { ClusterTable, kindsText, Swatch } from "./cluster-table";

function ClusterNotFound({ project, id }: { project: string; id: string }) {
  const t = useTranslations("kg.cluster");
  return (
    <StatePanel
      icon={FileQuestion}
      title={id ? t("notFound") : t("missingId")}
      description={id ? <code className="block font-mono text-xs break-all text-foreground">{id}</code> : undefined}
      testId="state-not-found"
    >
      <Button asChild variant="outline">
        <Link href={explorerHref(project, { kind: "overview" })}>{t("back")}</Link>
      </Button>
    </StatePanel>
  );
}

function Members({ project, detail }: { project: string; detail: ClusterDetail }) {
  const t = useTranslations("kg.cluster");
  const [text, setText] = useState("");
  const parts = useMemo(() => new Map(detail.subclusters.map((c) => [c.id, c.label])), [detail.subclusters]);
  const shown = useMemo(() => filterMembers(detail.members, text), [detail.members, text]);
  const columns = useMemo(() => {
    const helper = dataTableColumns<MapNode>();
    return helper.columns([
      helper.accessor((row) => row.name ?? row.id, {
        id: "node",
        header: () => t("columns.node"),
        sortFn: "text",
        cell: (info) => (
          <div className="flex min-w-0 flex-col gap-0.5">
            <Link href={nodeHref(project, info.row.original.id)} className={NAME_LINK} data-testid="kg-member-link">
              {info.getValue()}
            </Link>
            <span className="font-mono text-xs break-all whitespace-normal text-muted-foreground">
              {info.row.original.id}
            </span>
          </div>
        ),
      }),
      helper.accessor("kind", {
        header: () => t("columns.kind"),
        sortFn: "text",
        cell: (info) => <KindBadge kind={info.getValue()} />,
      }),
      helper.accessor((row) => (row.sub ? (parts.get(row.sub) ?? row.sub) : ""), {
        id: "part",
        header: () => t("columns.part"),
        sortFn: "text",
        cell: (info) => <span className="text-xs text-muted-foreground">{info.getValue() || "-"}</span>,
      }),
    ]);
  }, [t, project, parts]);
  return (
    <section aria-labelledby="kg-members-title" className="flex flex-col gap-3">
      <h2 id="kg-members-title" className="text-[15px] leading-[22px] font-semibold">
        {t("members")}
      </h2>
      <label className="flex max-w-sm flex-col gap-1 text-sm">
        <span>{t("filter")}</span>
        <Input
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder={t("filterPlaceholder")}
          data-testid="kg-member-filter"
        />
      </label>
      <DataTable
        data={shown}
        columns={columns}
        caption={t("membersCaption", { shown: shown.length, count: detail.members.length })}
        getRowId={(row) => row.id}
        density="compact"
        columnClassNames={{ part: "hidden md:table-cell" }}
        testId="kg-member-table"
      />
    </section>
  );
}

function Neighbours({ project, detail }: { project: string; detail: ClusterDetail }) {
  const t = useTranslations("kg.cluster");
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          <h2>{t("neighbours")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {detail.neighbours.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("noNeighbours")}</p>
        ) : (
          <ul className="flex flex-col gap-2" data-testid="kg-neighbour-clusters">
            {detail.neighbours.map((n) => (
              <li key={n.id} className="flex items-baseline justify-between gap-3 text-sm">
                <Link href={clusterHref(project, n.id)} className={NAME_LINK}>
                  {n.label || n.id}
                </Link>
                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                  {t("neighbourWeight", { count: n.weight })}
                </span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function Cluster({ project, detail }: { project: string; detail: ClusterDetail }) {
  const t = useTranslations("kg.cluster");
  const { cluster } = detail;
  return (
    <>
      <PageHeader
        title={<span data-testid="kg-cluster-title">{cluster.label || cluster.id}</span>}
        tags={
          <>
            <Tag>
              <Swatch index={0} />
              {t("level", { level: String(cluster.level) })}
            </Tag>
            <Tag>{t("size", { count: cluster.size })}</Tag>
          </>
        }
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href={explorerHref(project, { kind: "cluster", id: cluster.id })} data-testid="kg-cluster-explore">
              <Telescope aria-hidden="true" />
              {t("explore")}
            </Link>
          </Button>
        }
        sub={
          <span className="flex flex-col gap-1">
            <span className="font-mono text-xs break-all">{cluster.id}</span>
            <span className="text-xs">{kindsText(cluster.kinds)}</span>
            {cluster.parent ? (
              <Link href={clusterHref(project, cluster.parent)} className="text-xs text-brand hover:underline">
                {t("parent", { label: cluster.parent })}
              </Link>
            ) : null}
          </span>
        }
      />
      <Button asChild variant="link" className="w-fit px-0">
        <Link href={explorerHref(project, { kind: "overview" })}>
          <ArrowLeft aria-hidden="true" />
          {t("back")}
        </Link>
      </Button>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)] lg:items-start">
        <div className="flex min-w-0 flex-col gap-6">
          {detail.subclusters.length > 0 ? (
            <section aria-labelledby="kg-subclusters-title" className="flex flex-col gap-2">
              <h2 id="kg-subclusters-title" className="text-[15px] leading-[22px] font-semibold">
                {t("subclusters")}
              </h2>
              <ClusterTable project={project} clusters={detail.subclusters} testId="kg-subcluster-table" />
            </section>
          ) : null}
          <Members project={project} detail={detail} />
        </div>
        <Neighbours project={project} detail={detail} />
      </div>
      <ClusterDecisions project={project} ids={detail.members.map((member) => member.id)} />
    </>
  );
}

/** /p/{project}/kg/cluster?id=: one cluster, its parts, its members (filterable) and its neighbours; the explorer draws it. */
export function KgClusterPage({ project, id, error }: { project: string; id: string; error: ApiErrorInfo | null }) {
  const state = useHubQuery({ ...kgClusterQuery(browserApi, project, id), enabled: id !== "" }, error);
  if (!id || (state.status === "error" && state.error.status === 404)) {
    return <ClusterNotFound project={project} id={id} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(detail) => <Cluster project={project} detail={detail} />}
    </QueryView>
  );
}
