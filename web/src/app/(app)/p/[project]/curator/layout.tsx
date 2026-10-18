import type { ReactNode } from "react";

import { requireFeature } from "@/lib/hub-settings-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the project's Curator: a 404 while a hub admin has the curator feature off for the project, or for
 * the hub, as the API's routes for it answer.
 */
export default async function CuratorFeatureLayout({ children, params }: Props) {
  await requireFeature("curator", decodeURIComponent((await params).project));
  return children;
}
