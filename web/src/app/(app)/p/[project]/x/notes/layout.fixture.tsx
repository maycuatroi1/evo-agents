import type { ReactNode } from "react";

import { requirePlugin } from "@/lib/plugins-server";

type Props = { children: ReactNode; params: Promise<{ project: string }> };

/**
 * Every page of the core plugin notes, the tests' (src/components/plugin-notes): a 404 in a project that has not
 * enabled the plugin, as its routes answer. A `.fixture.tsx` route: only a build with EVO_WEB_FIXTURES=1 (the
 * end-to-end tests') carries it.
 */
export default async function NotesLayout({ children, params }: Props) {
  await requirePlugin("notes", decodeURIComponent((await params).project), "entries");
  return children;
}
