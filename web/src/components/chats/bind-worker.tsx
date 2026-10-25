"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";

import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";

import { useChatFailure, useMe } from "./hooks";

/**
 * Under a refusal that the visitor has no worker taking the runs of `project`: one button for each worker of theirs
 * that is not revoked, which adds the project to it (PATCH /v1/workers/{id} with the projects it serves and this one).
 * The visitor then sends the message again; nothing here resends it.
 */
export function BindWorker({ project }: { project: string }) {
  const t = useTranslations("chats.bind");
  const me = useMe();
  const queryClient = useQueryClient();
  const failure = useChatFailure();
  const workers = useQuery({
    queryKey: ["chats", "bind-workers"],
    queryFn: ({ signal }) => call(browserApi().GET("/v1/workers", { signal })),
  });
  const write = useMutation({
    mutationFn: async (args: { id: number; projects: string[] }) => {
      const api = browserApi();
      const headers = await csrfHeaders(api);
      return call(api.PATCH("/v1/workers/{worker_id}", { params: { path: { worker_id: args.id } }, body: { projects: args.projects }, headers }));
    },
    onSettled: () => Promise.all([queryClient.invalidateQueries({ queryKey: ["chats", "bind-workers"] }), queryClient.invalidateQueries({ queryKey: ["workers"] })]),
  });

  if (!workers.data || !me) return null;
  const own = workers.data.filter((worker) => worker.owner === me && worker.status !== "revoked");
  if (own.length === 0) return <p className="w-full text-xs text-muted-foreground">{t("none")}</p>;

  const bind = async (id: number, name: string, projects: string[]) => {
    try {
      await write.mutateAsync({ id, projects: [...new Set([...projects, project])] });
    } catch (error) {
      notifyFailure(t("failed", { name, project }), failure(error));
      return;
    }
    notify({ tone: "success", text: t("done", { name, project }) });
  };

  return (
    <div className="flex w-full flex-wrap items-center gap-1.5" data-testid="bind-worker" data-project={project}>
      <span className="text-xs text-muted-foreground">{t("lead", { project })}</span>
      {own.map((worker) => (
        <Button key={worker.id} type="button" size="sm" variant="outline" disabled={write.isPending} onClick={() => bind(worker.id, worker.name, worker.projects)}>
          {t("button", { name: worker.name })}
        </Button>
      ))}
    </div>
  );
}
