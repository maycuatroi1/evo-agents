import { redirect } from "next/navigation";

import { CRM_CONTACTS, CRM_PLUGIN } from "@/components/crm/api";
import { pluginPageHref } from "@/lib/plugin-queries";

type Props = { params: Promise<{ project: string }> };

/** /p/{project}/x/crm: the contacts, the plugin's one page. */
export default async function CrmIndexRoute({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  redirect(pluginPageHref(project, CRM_PLUGIN, CRM_CONTACTS));
}
