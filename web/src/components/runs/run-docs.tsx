"use client";

import { useQueries } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useRef, useState } from "react";

import { InFrame } from "@/components/data/data-card";
import { OpenDocContext } from "@/components/data/doc-buttons";
import { DocPanel } from "@/components/data/doc-panel";
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet";
import { browserApi } from "@/lib/api/browser";
import { type DocVersion, mergeVersions, runDocVersionsQuery } from "@/lib/doc-versions";
import type { ApiSource } from "@/lib/queries";

/** How often an open document of a chat that goes on asks the hub for new versions: as often as the chat itself. */
const REFRESH_MS = 3000;

/**
 * The document panel of an author run's Chat tab (`run-chat.tsx`): a text file of a message opens in a sheet on the
 * right, at any width, as the chat's does below lg (`components/data/doc-panel.tsx`). Its versions are the hub's lists
 * of the runs the chat spans (the files of the members' messages and each run's outputs), asked again every 3 seconds
 * while the chat goes on, so a version the agent sends shows in the open panel. Close gives the focus back.
 */
export function RunDocs({
  project,
  runIds,
  live,
  title,
  children,
  api = browserApi,
}: {
  project: string;
  /** The runs of the chat: the run and those that resume it. */
  runIds: readonly number[];
  /** The chat goes on: new versions may come. */
  live: boolean;
  title: string;
  children: ReactNode;
  api?: ApiSource;
}) {
  const t = useTranslations("chats.doc");
  const [doc, setDoc] = useState<string | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const openDoc = useCallback((name: string) => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setDoc(name);
  }, []);
  return (
    <OpenDocContext.Provider value={openDoc}>
      {children}
      <Sheet open={doc !== null} onOpenChange={(open) => (open ? undefined : setDoc(null))}>
        <SheetContent
          side="right"
          showCloseButton={false}
          className="gap-0 p-0 data-[side=right]:w-full data-[side=right]:sm:max-w-2xl"
          onCloseAutoFocus={(event) => {
            // The sheet traps the focus until it has closed: only then can the button that opened it take it back.
            if (opener.current?.isConnected) {
              event.preventDefault();
              opener.current.focus();
            }
          }}
          data-testid="run-doc-sheet"
        >
          <SheetTitle className="sr-only">{t("sheet")}</SheetTitle>
          <SheetDescription className="sr-only">{t("sheetBody", { name: doc ?? "", title })}</SheetDescription>
          <InFrame>
            {doc !== null ? (
              <RunDocPanel key={doc} api={api} project={project} runIds={runIds} live={live} name={doc} onClose={() => setDoc(null)} />
            ) : null}
          </InFrame>
        </SheetContent>
      </Sheet>
    </OpenDocContext.Provider>
  );
}

function RunDocPanel({
  api,
  project,
  runIds,
  live,
  name,
  onClose,
}: {
  api: ApiSource;
  project: string;
  runIds: readonly number[];
  live: boolean;
  name: string;
  onClose: () => void;
}) {
  const t = useTranslations("chats.doc");
  const lists = useQueries({ queries: runIds.map((runId) => runDocVersionsQuery(api, project, runId, name, live ? REFRESH_MS : false)) });
  const versions = mergeVersions(...lists.map((list) => list.data ?? []));
  const authorOf = (version: DocVersion) =>
    version.author === null ? t("byAgent") : version.author.kind === "agent" ? version.author.name : (version.author.login ?? version.author.name);
  return <DocPanel name={name} versions={versions} pending={lists.some((list) => list.isPending)} authorOf={authorOf} onClose={onClose} className="h-full" />;
}
