import { dehydrate } from "@tanstack/react-query";
import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NotesPage } from "@/components/plugin-notes/notes-page";
import { HydrationBoundary } from "@/lib/api/hydration-boundary";
import { getQueryClient } from "@/lib/api/prefetch";
import { requirePlugin } from "@/lib/plugins-server";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("plugins.notes");
  return { title: t("metaTitle", { project: decodeURIComponent((await params).project) }) };
}

/** The native page entries of the core plugin notes: the layout has checked the project enabled it. */
export default async function NotesEntriesPage({ params }: Props) {
  const project = decodeURIComponent((await params).project);
  const plugin = await requirePlugin("notes", project, "entries");
  const title = plugin?.pages.find((page) => page.name === "entries")?.title ?? "Notes";
  return (
    <HydrationBoundary state={dehydrate(getQueryClient())}>
      <NotesPage project={project} title={title} />
    </HydrationBoundary>
  );
}
