import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";

import { NewDocumentPage } from "@/components/docs/edit-page";

import { projectParam } from "../route-params";

type Props = { params: Promise<{ project: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const t = await getTranslations("docs.meta");
  return { title: t("new", { project: decodeURIComponent((await params).project) }) };
}

/** A new document of the project. */
export default async function DocsNewRoute({ params }: Props) {
  return <NewDocumentPage project={await projectParam(params)} />;
}
