import "server-only";

import { notFound } from "next/navigation";

import { SLUG } from "@/components/docs/model";
import { PROJECT_NAME } from "@/lib/queries";

/** The project and document a route of the feature docs names, decoded; a 404 for a name the hub would refuse. */
export async function documentParams(params: Promise<{ project: string; slug: string }>) {
  const { project, slug } = await params;
  const names = { project: decodeURIComponent(project), slug: decodeURIComponent(slug) };
  if (!PROJECT_NAME.test(names.project) || !SLUG.test(names.slug)) notFound();
  return names;
}

/** The project a route of the feature docs names, decoded; a 404 for a name the hub would refuse. */
export async function projectParam(params: Promise<{ project: string }>) {
  const project = decodeURIComponent((await params).project);
  if (!PROJECT_NAME.test(project)) notFound();
  return project;
}
