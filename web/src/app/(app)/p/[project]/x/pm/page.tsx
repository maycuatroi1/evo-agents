import { redirect } from "next/navigation";

import { PM_DAILY, PM_PLUGIN } from "@/components/pm/api";
import { pluginPageHref } from "@/lib/plugin-queries";

type Props = { params: Promise<{ project: string }> };

/** The link the hub's digest carries (/p/{project}/x/pm): the daily report, the plugin's one page. */
export default async function PmIndexRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  redirect(pluginPageHref(project, PM_PLUGIN, PM_DAILY));
}
