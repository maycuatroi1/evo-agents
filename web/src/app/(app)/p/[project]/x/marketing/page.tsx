import { redirect } from "next/navigation";

import { MARKETING_OVERVIEW, MARKETING_PLUGIN } from "@/components/marketing/api";
import { pluginPageHref } from "@/lib/plugin-queries";

type Props = { params: Promise<{ project: string }> };

/** /p/{project}/x/marketing: the overview, the first of the plugin's pages. */
export default async function MarketingIndexRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  redirect(pluginPageHref(project, MARKETING_PLUGIN, MARKETING_OVERVIEW));
}
