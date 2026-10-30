"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DocPanel } from "@/components/data/doc-panel";
import { browserApi } from "@/lib/api/browser";
import { chatDocVersionsQuery, type DocVersion, mergeVersions } from "@/lib/doc-versions";
import type { ApiSource } from "@/lib/queries";

import { docVersions } from "./doc-model";
import type { Feed } from "./feed-model";
import { useAuthorLabel } from "./message-parts";

/**
 * A text document of a chat, read beside it (web/DESIGN.md, Chats; `components/data/doc-panel.tsx`). Its versions are
 * the hub's list for the chat, read when the panel opens, so the versions in messages the page has not loaded count
 * too, merged with those the page holds, so a version the stream brings shows at once.
 */
export function ChatDocPanel({
  chatId,
  feed,
  name,
  me,
  onClose,
  className,
  api = browserApi,
}: {
  chatId: number;
  feed: Feed;
  name: string;
  me: string | null;
  onClose: () => void;
  className?: string;
  api?: ApiSource;
}) {
  const t = useTranslations("chats.doc");
  const authorLabel = useAuthorLabel();
  const hub = useQuery(chatDocVersionsQuery(api, chatId, name));
  const held = useMemo(() => docVersions(feed, name), [feed, name]);
  const versions = useMemo(() => mergeVersions(hub.data ?? [], held), [hub.data, held]);
  const authorOf = (version: DocVersion) => (version.author ? authorLabel(version.author, me) : t("byAgent"));
  return <DocPanel name={name} versions={versions} pending={hub.isPending} authorOf={authorOf} onClose={onClose} className={className} />;
}
