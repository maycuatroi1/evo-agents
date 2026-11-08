"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil } from "lucide-react";
import { useTranslations } from "next-intl";
import { type KeyboardEvent, useRef, useState } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notifyFailure } from "@/components/feedback/toast";
import { Input } from "@/components/ui/input";
import { browserApi } from "@/lib/api/browser";
import { MAX_NAME_CHARS, specOf, type SystemOut } from "@/lib/systems";
import { putSystem, systemKeys } from "@/lib/systems-queries";

/**
 * A system's name as the title of its page. Someone who may change the system clicks it to rename it in place: Enter
 * or leaving the field saves the name as the next revision (the saved spec with the new name), Escape keeps the old
 * one, and a blank name changes nothing. A draft of the designer with other changes follows the rename
 * (use-designer.ts), so a rename never costs a conflict.
 */
export function SystemName({ project, system }: { project: string; system: SystemOut }) {
  const t = useTranslations("systems.rename");
  const failure = useWriteFailure();
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const done = useRef(false);
  const shown = system.name || system.slug;
  const rename = useMutation({
    mutationFn: (name: string) => putSystem(browserApi(), project, system.slug, { ...specOf(system.spec), name }, system.revision),
    onSuccess: (written) => {
      queryClient.setQueryData(systemKeys.one(project, written.slug), written);
      void queryClient.invalidateQueries({ queryKey: systemKeys.list(project) });
      setEditing(false);
    },
    onError: (error) => {
      done.current = false;
      notifyFailure(t("failed"), failure(error, { 409: t("conflict") }));
    },
  });

  if (!system.can_change) return shown;

  if (!editing) {
    return (
      <button
        type="button"
        onClick={() => {
          done.current = false;
          setDraft(system.name);
          setEditing(true);
        }}
        className="group inline-flex max-w-full items-center gap-2 rounded-xs text-left [overflow-wrap:anywhere] hover:text-foreground/80"
        title={t("hint")}
        data-testid="system-name"
      >
        <span>{shown}</span>
        <Pencil className="size-4 shrink-0 text-muted-foreground opacity-60 group-hover:opacity-100 group-focus-visible:opacity-100" aria-hidden="true" />
      </button>
    );
  }

  const commit = () => {
    if (done.current || rename.isPending) return;
    const name = draft.trim();
    if (!name || name === system.name.trim() || [...name].length > MAX_NAME_CHARS) {
      setEditing(false);
      return;
    }
    done.current = true;
    rename.mutate(name);
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      done.current = true;
      setEditing(false);
    }
  };
  return (
    <Input
      value={draft}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={onKeyDown}
      onFocus={(event) => event.currentTarget.select()}
      maxLength={MAX_NAME_CHARS}
      readOnly={rename.isPending}
      aria-busy={rename.isPending || undefined}
      aria-label={t("field")}
      autoFocus
      className="h-9 max-w-md text-xl font-semibold tracking-tight md:text-xl"
      data-testid="system-name-input"
    />
  );
}
