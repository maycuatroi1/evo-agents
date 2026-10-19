import type { ReactNode } from "react";

import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin srs (src/components/srs): a 404 in a project that has not enabled the plugin, or where
 * the hub turned it off, as its routes under /v1/x/srs answer.
 */
export default async function SrsLayout({ children, params }: Props) {
  await requirePlugin("srs", decodeURIComponent((await params).project), "documents");
  return children;
}
