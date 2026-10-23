import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ProblemPage } from "@/components/loop/problem-page";
import { NotFoundState } from "@/components/states/states";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient, prefetch } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";
import { loopItemsQuery } from "@/lib/loop-queries";
import { PROBLEM_REF, problemQuery, problemRevisionsQuery } from "@/lib/problem-queries";
import { PROJECT_NAME } from "@/lib/queries";

type Props = { params: Promise<{ project: string; ref: string }> };

async function names(params: Props["params"]) {
  const { project, ref } = await params;
  return { project: decodeURIComponent(project), ref: decodeURIComponent(ref) };
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("problems.page");
  const { project, ref } = await names(params);
  return { title: t("meta", { project, code: ref }) };
}

/** One problem of a project, with its revisions and the Loop's work items that go from it, read on the server. */
export default async function ProjectProblemPage({ params }: Props) {
  const { project, ref } = await names(params);
  const t = await getTranslations("states");
  const problem = await getTranslations("problems.page");
  if (!PROJECT_NAME.test(project)) {
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  if (!PROBLEM_REF.test(ref)) return <NotFoundState title={problem("notFoundTitle", { code: ref })} description={problem("notFoundText")} />;
  const api = await serverApi();
  const client = getQueryClient();
  const [error] = await Promise.all([
    prefetch(client, problemQuery(() => api, project, ref)),
    prefetch(client, problemRevisionsQuery(() => api, project, ref)),
    prefetch(client, loopItemsQuery(() => api, project, { state: null, kind: null, goal: null, open: false, problem: ref })),
  ]);
  return (
    <HydrationBoundary state={dehydrate(client)}>
      <ProblemPage project={project} code={ref} initialError={error} />
    </HydrationBoundary>
  );
}
