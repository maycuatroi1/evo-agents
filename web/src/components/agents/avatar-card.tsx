"use client";

import { Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { AgentAvatar } from "@/components/data/agent-avatar";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { Agent } from "@/lib/agents";
import { fileBase64 } from "@/lib/avatars";

import { AvatarPicker } from "./form-parts";
import { useAgentFailure, useAvatarWrite } from "./hooks";

/**
 * The agent's face on its page: the avatar, or its initial on its colour. An admin of the project uploads an image
 * (PNG, JPEG or WebP, at most 512 KiB), which applies at once as the agent's next version, or removes it after a
 * confirmation; each result is a toast. A deleted agent keeps its face as it was.
 */
export function AvatarCard({ project, agent, admin }: { project: string; agent: Agent; admin: boolean }) {
  const t = useTranslations("agents.detail.avatar");
  const failure = useAgentFailure();
  const write = useAvatarWrite(project);
  const [removing, setRemoving] = useState(false);
  const editable = admin && agent.deleted_at === null;

  const upload = async (file: File) => {
    try {
      const saved = await write.mutateAsync({ handle: agent.handle, data: await fileBase64(file) });
      notify({ tone: "success", text: t("uploaded", { handle: agent.handle }), description: t("version", { version: saved.version }) });
    } catch (error) {
      notifyFailure(t("uploadFailed", { handle: agent.handle }), failure(error));
    }
  };
  const remove = async () => {
    try {
      const saved = await write.mutateAsync({ handle: agent.handle, data: null });
      setRemoving(false);
      notify({ tone: "success", text: t("removed", { handle: agent.handle }), description: t("version", { version: saved.version }) });
    } catch {
      // shown in the confirmation, which stays open
    }
  };

  return (
    <Card data-testid="agent-avatar-card">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-wrap items-center gap-4">
        <AgentAvatar agent={agent} size="xl" label={t("label", { name: agent.name })} />
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <p className="text-[13px] text-pretty text-muted-foreground">{agent.icon_url ? t("image") : t("initial")}</p>
          {editable ? (
            <div className="flex flex-wrap gap-2">
              <AvatarPicker label={agent.icon_url ? t("change") : t("upload")} onPick={(file) => void upload(file)} busy={write.isPending && !removing} disabled={write.isPending} />
              {agent.icon_url ? (
                <Button type="button" variant="quiet-danger" onClick={() => setRemoving(true)} disabled={write.isPending} data-testid="agent-avatar-remove">
                  <Trash2 aria-hidden="true" />
                  {t("remove")}
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
      </CardContent>
      <ConfirmAction
        open={removing}
        onOpenChange={(open) => {
          if (open) write.reset();
          setRemoving(open);
        }}
        title={t("removeTitle", { handle: agent.handle })}
        description={<p>{t("removeText")}</p>}
        confirmLabel={t("removeConfirm")}
        pendingLabel={t("removing")}
        cancelLabel={t("cancel")}
        pending={write.isPending}
        error={removing && write.isError ? failure(write.error) : null}
        onConfirm={() => void remove()}
        testId="agent-avatar-remove-dialog"
      />
    </Card>
  );
}
