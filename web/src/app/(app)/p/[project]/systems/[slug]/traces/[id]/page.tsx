import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotFoundState } from "@/components/states/states";
import { TracePage } from "@/components/systems/trace-page";
import { PROJECT_NAME } from "@/lib/queries";
import { SLUG } from "@/lib/systems";

type Props = { params: Promise<{ project: string; slug: string; id: string }> };

const TRACE_ID = /^[1-9][0-9]{0,17}$/;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("systems.traceView");
  const { project, slug, id } = await params;
  return { title: t("metaTitle", { id, slug: decodeURIComponent(slug), project: decodeURIComponent(project) }) };
}

/**
 * One trace of an agent system, opened from the recent traces of its Observe mode: the work of each agent and the
 * waterfall of its spans, kept current by the system's trace stream. The trace loads in the browser, as Observe's
 * reads do.
 */
export default async function SystemTracePage({ params }: Props) {
  const { project: rawProject, slug: rawSlug, id } = await params;
  const project = decodeURIComponent(rawProject);
  const slug = decodeURIComponent(rawSlug);
  if (!PROJECT_NAME.test(project)) {
    const t = await getTranslations("states");
    return <NotFoundState title={t("projectNotFoundTitle", { name: project })} description={t("projectNotFoundDescription")} />;
  }
  if (!SLUG.test(slug) || !TRACE_ID.test(id)) {
    const s = await getTranslations("systems");
    return <NotFoundState title={s("notFoundTitle", { slug })} description={s("notFoundDescription")} />;
  }
  return <TracePage project={project} slug={slug} id={Number(id)} />;
}
