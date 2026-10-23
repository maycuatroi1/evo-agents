import type { ReactNode } from "react";

import { MARKETING_OVERVIEW, MARKETING_PLUGIN } from "@/components/marketing/api";
import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin marketing (src/components/marketing): a 404 in a project that has not enabled the
 * plugin, or where the hub turned it off, as its route under /v1/x/marketing answers.
 */
export default async function MarketingLayout({ children, params }: Props) {
  await requirePlugin(MARKETING_PLUGIN, decodeURIComponent((await params).project), MARKETING_OVERVIEW);
  return children;
}
