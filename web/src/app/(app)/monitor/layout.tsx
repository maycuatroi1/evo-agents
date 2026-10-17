import type { ReactNode } from "react";

import { requireFeature } from "@/lib/hub-settings-server";

/** The Monitor: a 404 while a hub admin has the monitor feature off for the hub. */
export default async function MonitorFeatureLayout({ children }: { children: ReactNode }) {
  await requireFeature("monitor");
  return children;
}
