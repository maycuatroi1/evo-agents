import type { ReactNode } from "react";

import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the hub's feature docs (src/components/docs): a 404 in a project the feature is off in, as its routes
 * under /v1/x/docs answer.
 */
export default async function DocsLayout({ children, params }: Props) {
  await requirePlugin("docs", decodeURIComponent((await params).project), "documents");
  return children;
}
