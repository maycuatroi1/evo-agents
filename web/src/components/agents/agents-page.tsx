"use client";

import { Bot, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { DataCard, DataToolbar } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { FacetGroup } from "@/components/data/facet-group";
import { NAME_LINK } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useCanChat } from "@/hooks/use-direct-chat";
import { agentHref, agentsQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AgentDialog } from "./agent-dialog";
import { AxisTags } from "./axis";
import { ChatWithAgent } from "./chat-button";
import { useAgentAdmin } from "./hooks";
import { AgentBadges, ReadOnlyNote } from "./parts";

const NARROW_HIDDEN = { limits: "hidden md:table-cell", version: "hidden sm:table-cell", updated: "hidden lg:table-cell" };

type Show = "active" | "deleted";

function AgentsTable({ project, agents, deleted }: { project: string; agents: Agent[]; deleted: boolean }) {
  const t = useTranslations("agents.list");
  // Chat opens the visitor's direct chat with an agent: for one who writes to the project, on the active agents.
  const chat = useCanChat(project) && !deleted;
  const columns = useMemo(() => {
    const helper = dataTableColumns<Agent>();
    const actions = helper.display({
      id: "actions",
      header: () => <span className="sr-only">{t("columns.actions")}</span>,
      meta: { actions: true },
      cell: (info) => <ChatWithAgent agent={info.row.original} compact />,
    });
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.agent"),
        sortFn: "alphanumeric",
        meta: { primary: true },
        cell: (info) => {
          const agent = info.row.original;
          const sub = agent.description.trim() ? `@${agent.handle}, ${agent.description.trim()}` : `@${agent.handle}`;
          return (
            <div className="flex w-0 min-w-full items-center gap-3">
              <AgentAvatar agent={agent} />
              <CellMain sub={sub} subTitle={sub} className="w-auto min-w-0 flex-1">
                <Link href={agentHref(project, agent.handle)} className={`${NAME_LINK} truncate`} data-agent-link={agent.handle}>
                  {agent.name}
                </Link>
                <AgentBadges agent={agent} />
              </CellMain>
            </div>
          );
        },
      }),
      helper.display({
        id: "limits",
        header: () => t("columns.limits"),
        cell: (info) => <AxisTags axes={info.row.original.restricted} />,
      }),
      helper.accessor("version", {
        header: () => t("columns.version"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span className="font-mono text-xs tabular-nums">{t("version", { version: info.getValue() })}</span>,
      }),
      helper.accessor((row) => new Date(deleted && row.deleted_at ? row.deleted_at : row.updated_at), {
        id: "updated",
        header: () => (deleted ? t("columns.deleted") : t("columns.updated")),
        sortFn: "datetime",
        meta: { numeric: true },
        cell: (info) => <When value={deleted ? info.row.original.deleted_at : info.row.original.updated_at} short />,
      }),
      ...(chat ? [actions] : []),
    ]);
  }, [t, project, deleted, chat]);

  return (
    <DataTable
      data={agents}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(row) => row.handle}
      columnClassNames={NARROW_HIDDEN}
      testId="agents-table"
      mobile={(agent) => ({
        title: agent.name,
        titleText: agent.name,
        href: agentHref(project, agent.handle),
        tags: <AgentBadges agent={agent} />,
        status: <AgentAvatar agent={agent} size="sm" />,
        meta: <span className="font-mono">@{agent.handle}</span>,
        metaText: `@${agent.handle}`,
        actions: chat ? <ChatWithAgent agent={agent} compact /> : undefined,
        data: { "agent-handle": agent.handle },
      })}
    />
  );
}

/**
 * A project's agents (web/DESIGN.md, Agents): each one's avatar, name and @handle, what it limits, its version and when
 * it last changed; the project's default first. Every member reads them; an admin of the project creates one from
 * here (New agent) and lands on its page. Deleted agents are one facet away, since old runs keep their names.
 */
export function AgentsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("agents");
  const router = useRouter();
  const params = useSearchParams();
  const state = useHubQuery(agentsQuery(browserApi, project), initialError);
  const admin = useAgentAdmin(project);
  const [creating, setCreating] = useState(false);
  const [show, setShow] = useState<Show>(params.get("show") === "deleted" ? "deleted" : "active");
  const agents = state.status === "success" ? state.data : null;
  const active = (agents ?? []).filter((agent) => agent.deleted_at === null);
  const deleted = (agents ?? []).filter((agent) => agent.deleted_at !== null);

  const select = (value: string | null) => {
    const next: Show = value === "deleted" ? "deleted" : "active";
    setShow(next);
    const url = new URL(window.location.href);
    if (next === "deleted") url.searchParams.set("show", "deleted");
    else url.searchParams.delete("show");
    window.history.replaceState(window.history.state, "", url);
  };

  const shown = show === "deleted" && deleted.length > 0 ? deleted : active;
  return (
    <div className="flex flex-col gap-6" data-testid="agents-page">
      <PageHeader
        title={t("title")}
        tags={agents ? <Badge variant="secondary">{t("count", { count: active.length })}</Badge> : null}
        actions={
          admin ? (
            <Button onClick={() => setCreating(true)} data-testid="agents-new">
              <Plus aria-hidden="true" />
              {t("newButton")}
            </Button>
          ) : null
        }
      />
      {admin ? null : <ReadOnlyNote project={project} />}
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {() => (
          <section aria-labelledby="agents-list-title">
            <h2 id="agents-list-title" className="sr-only">
              {t("list.caption", { project })}
            </h2>
            <DataCard
              testId="agents-card"
              toolbar={
                deleted.length > 0 ? (
                  <DataToolbar label={t("list.toolbar", { project })} count={t("list.shown", { count: shown.length })}>
                    <FacetGroup
                      label={t("list.facet")}
                      options={[
                        { value: null, label: t("list.active"), icon: Bot, count: active.length },
                        { value: "deleted", label: t("list.deletedFacet"), count: deleted.length },
                      ]}
                      selected={show === "deleted" ? "deleted" : null}
                      onSelect={select}
                      countLabel={(count) => t("count", { count })}
                      testId="agents-facet"
                    />
                  </DataToolbar>
                ) : null
              }
            >
              <AgentsTable project={project} agents={shown} deleted={shown === deleted} />
            </DataCard>
            {active.length === 1 && admin ? (
              <p className="mt-3 text-[13px] text-pretty text-muted-foreground" data-testid="agents-only-default">
                {t("list.onlyDefault")}
              </p>
            ) : null}
          </section>
        )}
      </QueryView>
      {admin ? (
        <AgentDialog
          project={project}
          agent={null}
          open={creating}
          onOpenChange={setCreating}
          onSaved={(agent) => router.push(agentHref(project, agent.handle))}
        />
      ) : null}
    </div>
  );
}
