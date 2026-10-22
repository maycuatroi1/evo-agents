import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { LazyAgentAccountsCard } from "@/components/accounts/agent-accounts-lazy";
import { AgentDetail } from "@/components/agents/agent-detail";
import { AgentRuns } from "@/components/runs/agent-runs";
import { NotFoundState } from "@/components/states/states";
import { agentQuery } from "@/lib/agent-queries";
import { HANDLE } from "@/lib/agent-rules";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; handle: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: `@${decodeURIComponent((await params).handle)}` };
}

/**
 * One agent of a project. The page composes the agent's own view with its latest runs, which the runs feature draws,
 * and its accounts, which the accounts feature draws, so no feature imports another.
 */
export default async function ProjectAgentPage({ params }: Props) {
  const { project: rawProject, handle: rawHandle } = await params;
  const project = decodeURIComponent(rawProject);
  const handle = decodeURIComponent(rawHandle).replace(/^@/, "");
  if (!PROJECT_NAME.test(project) || !HANDLE.test(handle)) {
    const t = await getTranslations("agents.detail");
    return <NotFoundState title={t("notFoundTitle", { handle })} description={t("notFoundDescription", { project })} />;
  }
  const api = await serverApi();
  const client = getQueryClient();
  const error = await prefetch(client, agentQuery(() => api, project, handle));
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <AgentDetail
        project={project}
        handle={handle}
        initialError={error}
        runs={<AgentRuns project={project} handle={handle} />}
        accounts={<LazyAgentAccountsCard project={project} handle={handle} />}
      />
    </HydrationBoundary>
  );
}
