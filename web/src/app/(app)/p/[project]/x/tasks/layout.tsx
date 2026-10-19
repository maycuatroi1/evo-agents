import type { ReactNode } from "react";

import { TASKS_BOARD, TASKS_PLUGIN } from "@/lib/core-plugin-links";
import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin tasks (src/components/tasks): a 404 in a project that has not enabled the plugin, or
 * where the hub turned it off, as its routes under /v1/x/tasks answer.
 */
export default async function TasksLayout({ children, params }: Props) {
  await requirePlugin(TASKS_PLUGIN, decodeURIComponent((await params).project), TASKS_BOARD);
  return children;
}
