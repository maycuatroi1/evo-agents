"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { type Audience, type SkillHistory, skillKeys, setSkillAudience } from "./queries";

/**
 * Who reads a global skill (docs/hub.md, Skills): every signed-in member, or hub admins alone. A hub admin flips it
 * here, and it applies at once (PUT /v1/skills/global/{name}/audience, one audit row per change). Nobody else sees the
 * card: a member never reads a skill that is for hub admins, and the API refuses them the write.
 */
export function SkillAudienceCard({ skill }: { skill: SkillHistory }) {
  const { data: me } = useQuery(whoamiQuery(browserApi));
  if (skill.project !== null || !me?.admin) return null;
  return <AudienceSwitch skill={skill} />;
}

function AudienceSwitch({ skill }: { skill: SkillHistory }) {
  const t = useTranslations("skills.audience");
  const id = useId();
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const change = useMutation({
    mutationFn: (audience: Audience) => setSkillAudience(browserApi(), skill.name, audience),
    onSuccess: (saved) => {
      queryClient.setQueryData(skillKeys.one({ kind: "global" }, skill.name), (old: SkillHistory | undefined) =>
        old ? { ...old, audience: saved.audience } : old,
      );
      notify({
        tone: "success",
        text: saved.audience === "admins" ? t("nowAdmins", { name: saved.name }) : t("nowEveryone", { name: saved.name }),
      });
    },
    onError: (error) => notifyFailure(t("failed", { name: skill.name }), failure(error)),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["skills"] }),
  });
  const audience = change.isPending ? (change.variables ?? skill.audience) : skill.audience;
  return (
    <Card data-testid="skill-audience">
      <CardHeader>
        <CardTitle>
          <h2>{t("title")}</h2>
        </CardTitle>
        <CardDescription>{t("description")}</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-start justify-between gap-4">
          <div className="flex min-w-0 flex-col gap-0.5">
            <label htmlFor={id} className="text-sm font-medium text-foreground">
              {t("adminsOnly")}
            </label>
            <p id={`${id}-hint`} className="text-[13px] leading-[18px] text-pretty text-muted-foreground">
              {t("adminsOnlyHint")}
            </p>
          </div>
          <Switch
            id={id}
            checked={audience === "admins"}
            disabled={change.isPending}
            aria-busy={change.isPending || undefined}
            aria-describedby={`${id}-hint`}
            onCheckedChange={(on) => change.mutate(on ? "admins" : "everyone")}
            className="mt-0.5"
            data-testid="skill-audience-switch"
          />
        </div>
      </CardContent>
    </Card>
  );
}
