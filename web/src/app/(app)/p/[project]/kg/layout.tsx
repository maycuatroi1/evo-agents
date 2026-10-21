import type { ReactNode } from "react";

import { requireFeature } from "@/lib/hub-settings-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the project's knowledge graph: a 404 while a hub admin has the kg feature off for the project, or for
 * the hub, as the API's routes for it answer.
 */
export default async function KgFeatureLayout({ children, params }: Props) {
  await requireFeature("kg", decodeURIComponent((await params).project));
  return children;
}
