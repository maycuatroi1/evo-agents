"use client";

import { Ban, Pencil, Trash2 } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { Identifier, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { agentQuery } from "@/lib/agent-queries";
import type { Agent } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { AgentDialog } from "./agent-dialog";
import { AgentHistory, DeleteAgent } from "./agent-history";
import { AgentPacks } from "./agent-packs";
import { AgentPermissions } from "./agent-permissions";
import { AvatarCard } from "./avatar-card";
import { ChatWithAgent } from "./chat-button";
import { useAgentAdmin } from "./hooks";
import { AgentBadges, ReadOnlyNote } from "./parts";

/**
 * One agent of a project (web/DESIGN.md, Agents): its face, name and @handle, what it is for and the instructions its
 * runs get, its eight axes and the agent packs it attaches, its versions, the runs that went under it (`runs`, which
 * the route composes from the runs feature) and, for an agent other than the default, its memories (`memories`, from
 * the memories feature). Chat opens the visitor's direct chat with it (for one who writes to
 * the project). An admin of the project edits it, changes its avatar and deletes it (never the default); everyone else
 * reads it. A deleted agent reads as before, under a note.
 */
export function AgentDetail({
  project,
  handle,
  initialError,
  runs,
  memories,
}: {
  project: string;
  handle: string;
  initialError: ApiErrorInfo | null;
  runs: ReactNode;
  memories: ReactNode;
}) {
  const t = useTranslations("agents.detail");
  const state = useHubQuery(agentQuery(browserApi, project, handle), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { handle })} description={t("notFoundDescription", { project })} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(agent) => <AgentPage project={project} agent={agent} runs={runs} memories={memories} />}
    </QueryView>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className="min-w-0 text-[13px] text-foreground [overflow-wrap:anywhere]">{children}</dd>
    </div>
  );
}

function AgentPage({ project, agent, runs, memories }: { project: string; agent: Agent; runs: ReactNode; memories: ReactNode }) {
  const t = useTranslations("agents.detail");
  const format = useFormatter();
  const admin = useAgentAdmin(project);
  const [editing, setEditing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const live = agent.deleted_at === null;
  const created = format.dateTime(new Date(agent.created_at), { dateStyle: "medium" });

  return (
    <div className="flex flex-col gap-6" data-testid="agent-detail" data-handle={agent.handle} data-version={agent.version}>
      <PageHeader
        leading={<AgentAvatar agent={agent} size="lg" />}
        title={<span data-testid="agent-name">{agent.name}</span>}
        tags={
          <>
            <Identifier value={`@${agent.handle}`} copy copyLabel={t("copyHandle")} testId="agent-handle" />
            <AgentBadges agent={agent} />
            <Tag data-testid="agent-version">{t("version", { version: agent.version })}</Tag>
          </>
        }
        sub={t("sub", { login: agent.created_by, date: created })}
        actions={
          <>
            <ChatWithAgent agent={agent} />
            {admin && live ? (
              <>
                <Button variant="outline" onClick={() => setEditing(true)} data-testid="agent-edit">
                  <Pencil aria-hidden="true" />
                  {t("edit")}
                </Button>
                {agent.is_default ? null : (
                  <Button variant="quiet-danger" onClick={() => setDeleting(true)} data-testid="agent-delete">
                    <Trash2 aria-hidden="true" />
                    {t("delete.button")}
                  </Button>
                )}
              </>
            ) : null}
          </>
        }
      />
      {live ? null : (
        <p role="note" className="flex items-start gap-2.5 rounded-md border bg-surface-sunken px-4 py-3 text-[13px] text-pretty text-muted-foreground" data-testid="agent-deleted-note">
          <Ban className="mt-px size-4 shrink-0" aria-hidden="true" />
          <span>
            {t("deletedNote")}{" "}
            <span className="whitespace-nowrap">
              {t("deletedAt")} <When value={agent.deleted_at} />
            </span>
          </span>
        </p>
      )}
      {admin ? null : <ReadOnlyNote project={project} />}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="flex min-w-0 flex-col gap-4">
          <Card data-testid="agent-about">
            <CardHeader>
              <CardTitle>
                <h2>{t("about.title")}</h2>
              </CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <p className="text-sm whitespace-pre-line text-pretty text-foreground [overflow-wrap:anywhere]" data-testid="agent-description">
                {agent.description.trim() || <span className="text-muted-foreground">{t("about.noDescription")}</span>}
              </p>
              <div className="flex flex-col gap-1.5">
                <h3 className="text-xs font-medium text-muted-foreground">{t("about.instructions")}</h3>
                {agent.instructions.trim() ? (
                  <pre className="max-h-80 overflow-auto rounded-md border bg-surface-sunken px-3 py-2 font-mono text-[13px] leading-5 whitespace-pre-wrap text-foreground [overflow-wrap:anywhere]" tabIndex={0} data-testid="agent-instructions">
                    {agent.instructions}
                  </pre>
                ) : (
                  <p className="text-[13px] text-muted-foreground" data-testid="agent-instructions">
                    {agent.is_default ? t("about.defaultInstructions") : t("about.noInstructions")}
                  </p>
                )}
              </div>
            </CardContent>
          </Card>
          <AgentPermissions agent={agent} />
          <AgentPacks agent={agent} />
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <AvatarCard project={project} agent={agent} admin={admin} />
          <Card data-testid="agent-facts">
            <CardHeader>
              <CardTitle>
                <h2>{t("facts.title")}</h2>
              </CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-2 gap-x-4 gap-y-3 xl:grid-cols-1 2xl:grid-cols-2">
                <Fact label={t("facts.handle")}>
                  <span className="font-mono">@{agent.handle}</span>
                </Fact>
                <Fact label={t("facts.project")}>{agent.project}</Fact>
                <Fact label={t("facts.createdBy")}>{agent.created_by}</Fact>
                <Fact label={t("facts.updated")}>
                  <When value={agent.updated_at} />
                </Fact>
              </dl>
            </CardContent>
          </Card>
          <AgentHistory project={project} agent={agent} />
        </div>
      </div>

      {agent.is_default ? null : (
        <Card data-testid="agent-memory-card">
          <CardHeader>
            <CardTitle>
              <h2>{t("memories.title")}</h2>
            </CardTitle>
          </CardHeader>
          <CardContent>{memories}</CardContent>
        </Card>
      )}

      <Card data-testid="agent-runs">
        <CardHeader>
          <CardTitle>
            <h2>{t("runs.title")}</h2>
          </CardTitle>
        </CardHeader>
        <CardContent>{runs}</CardContent>
      </Card>

      {admin && live ? (
        <>
          <AgentDialog project={project} agent={agent} open={editing} onOpenChange={setEditing} />
          {agent.is_default ? null : <DeleteAgent project={project} agent={agent} open={deleting} onOpenChange={setDeleting} />}
        </>
      ) : null}
    </div>
  );
}
