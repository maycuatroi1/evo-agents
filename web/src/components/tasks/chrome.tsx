"use client";

import { ClipboardList, Columns3, Eye, ListChecks, Upload } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { TabNav } from "@/components/data/tab-nav";
import { Banner } from "@/components/feedback/banner";

import { backlogHref, boardHref, importHref, mineHref } from "./links";

export type TasksTab = "board" | "backlog" | "mine" | "import";

/** The tabs of the plugin's pages: the board, the backlog, the visitor's own tasks, and the import for writers. */
export function TasksTabs({ project, current, writer }: { project: string; current: TasksTab | null; writer: boolean }) {
  const t = useTranslations("tasks.tabs");
  const tabs = [
    { id: "board", label: t("board"), href: boardHref(project), icon: Columns3 },
    { id: "backlog", label: t("backlog"), href: backlogHref(project), icon: ListChecks },
    { id: "mine", label: t("mine"), href: mineHref(project), icon: ClipboardList },
    ...(writer || current === "import" ? [{ id: "import", label: t("import"), href: importHref(project), icon: Upload }] : []),
  ];
  return <TabNav label={t("label")} tabs={tabs} current={current} testId="tasks-tabs" tabTestId="tasks-tab" />;
}

/** What a reader of the project may do on these pages: read, never write. */
export function ReaderNote({ project, children }: { project: string; children: ReactNode }) {
  const t = useTranslations("tasks");
  return (
    <Banner icon={Eye} title={t("readerTitle", { project })} testId="tasks-read-only">
      {children}
    </Banner>
  );
}
