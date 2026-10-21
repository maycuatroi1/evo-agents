import type { ReactNode } from "react";

import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin canvas (src/components/canvas): a 404 in a project that has not enabled the plugin, or
 * where the hub turned it off, as its routes under /v1/x/canvas answer.
 */
export default async function CanvasLayout({ children, params }: Props) {
  await requirePlugin("canvas", decodeURIComponent((await params).project), "canvases");
  return children;
}
