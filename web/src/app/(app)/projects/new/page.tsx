import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NewProjectPage } from "@/components/kg/upload/new-project-page";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("kgUpload.newProject");
  return { title: t("title") };
}

/** /projects/new, New project from data: the hub creates a project and reads the archive uploaded into it. */
export default function NewProjectRoute() {
  return <NewProjectPage />;
}
