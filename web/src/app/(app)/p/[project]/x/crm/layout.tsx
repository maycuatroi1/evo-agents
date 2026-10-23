import type { ReactNode } from "react";

import { CRM_CONTACTS, CRM_PLUGIN } from "@/components/crm/api";
import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin crm (src/components/crm): a 404 in a project that has not enabled the plugin, or where
 * the hub turned it off, as its route under /v1/x/crm answers.
 */
export default async function CrmLayout({ children, params }: Props) {
  await requirePlugin(CRM_PLUGIN, decodeURIComponent((await params).project), CRM_CONTACTS);
  return children;
}
