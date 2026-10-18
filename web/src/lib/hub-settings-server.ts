import "server-only";

import { notFound } from "next/navigation";

import { getQueryClient } from "@/lib/api/prefetch";
import { serverApi } from "@/lib/api/server";

import { type Branding, brandingQuery, type FeatureId, featureOn, featuresQuery } from "./hub-settings";

/**
 * The hub's branding for a server component (the root layout, the sign-in page), read once per request through the
 * request's QueryClient, which the signed-in shell then hands to the browser. Null when the API does not answer: the
 * web shows its own name, mark and colours, as without branding.
 */
export async function serverBranding(): Promise<Branding | null> {
  try {
    const api = await serverApi();
    return await getQueryClient().fetchQuery(brandingQuery(() => api));
  } catch {
    return null;
  }
}

/**
 * The page of a feature a hub admin turned off (in `project`, or for the hub) is a 404, as the API's routes for it
 * are. When the features cannot be read the page goes on, and answers for itself.
 */
export async function requireFeature(id: FeatureId, project?: string): Promise<void> {
  let on = true;
  try {
    const api = await serverApi();
    on = featureOn(await getQueryClient().fetchQuery(featuresQuery(() => api)), id, project);
  } catch {
    return;
  }
  if (!on) notFound();
}
