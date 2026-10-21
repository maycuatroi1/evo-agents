import type { ReactNode } from "react";

import { PM_DAILY, PM_PLUGIN } from "@/components/pm/api";
import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin pm (src/components/pm): a 404 in a project that has not enabled the plugin, or where
 * the hub turned it off, as its route under /v1/x/pm answers.
 */
export default async function PmLayout({ children, params }: Props) {
  await requirePlugin(PM_PLUGIN, decodeURIComponent((await params).project), PM_DAILY);
  return children;
}
