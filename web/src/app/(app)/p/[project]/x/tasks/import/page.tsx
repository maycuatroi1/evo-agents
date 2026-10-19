import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { ImportPage } from "@/components/tasks/import-page";
import { TASKS_PLUGIN } from "@/lib/core-plugin-links";
import { requirePlugin } from "@/lib/plugins-server";

import { pageTitle, projectParam } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("tasks.meta");
  return { title: t("import", { project: await projectParam(params) }) };
}

/** evo-tasks/1 JSON into the project's tasks, once, after the hub's dry run. */
export default async function TasksImportRoute({ params }: Props) {
  const project = await projectParam(params);
  const plugin = await requirePlugin(TASKS_PLUGIN, project, "import");
  return <ImportPage project={project} title={pageTitle(plugin, "import", "Import tasks")} />;
}
