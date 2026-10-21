import "server-only";

import { notFound } from "next/navigation";

import { CLAIM_CODE, SLUG } from "@/components/canvas/model";
import { PROJECT_NAME } from "@/lib/queries";

/** The project and canvas a route of the plugin canvas names, decoded; a 404 for a name the hub would refuse. */
export async function canvasParams(params: Promise<{ project: string; slug: string }>) {
  const { project, slug } = await params;
  const names = { project: decodeURIComponent(project), slug: decodeURIComponent(slug) };
  if (!PROJECT_NAME.test(names.project) || !SLUG.test(names.slug)) notFound();
  return names;
}

/** A claim's code as a route names it (`CL-3`, any case), as the hub writes it; a 404 for anything else. */
export function claimCode(ref: string): string {
  const code = decodeURIComponent(ref).toUpperCase();
  if (!CLAIM_CODE.test(code)) notFound();
  return code;
}

/** A baseline's number as a route names it; a 404 for anything that is no positive whole number. */
export function baselineNumber(value: string): number {
  if (!/^[1-9][0-9]{0,8}$/.test(value)) notFound();
  return Number(value);
}

/** The title the plugin's manifest gives page `page`, as the project's menu shows it. */
export function pageTitle(plugin: { pages: { name: string; title: string }[] } | null, page: string, fallback: string): string {
  return plugin?.pages.find((item) => item.name === page)?.title ?? fallback;
}
