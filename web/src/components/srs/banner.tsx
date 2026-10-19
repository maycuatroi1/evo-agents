"use client";

import { Eye } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Banner } from "@/components/feedback/banner";

/** What a reader of the project may do on this page: read, never write. */
export function ReaderNote({ project, children }: { project: string; children: ReactNode }) {
  const t = useTranslations("srs");
  return (
    <Banner icon={Eye} title={t("readerTitle", { project })} testId="srs-read-only">
      {children}
    </Banner>
  );
}
