"use client";

import { FlaskConical, PackageCheck, PackageX, Trophy, Undo2 } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";

import { useInsightFormat } from "./insight-format";
import { releaseOf, replayOf } from "./ledger-model";
import type { LedgerLine } from "./queries";
import { verdictOf } from "./replay-model";

/**
 * The parts of a ledger line the Curator's replays added (ledger.tsx): the verdict of the replays a merge of the hub
 * rests on, or that an outcome kept beside its figures, as arm b against arm a on the holdout tasks; and a skill
 * release, which global skill the hub published from the merge and which version stays, or published again.
 */

export function LineReplay({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger.replay");
  const format = useInsightFormat();
  const verdict = verdictOf(replayOf(line));
  if (verdict === null) return null;
  const mean = (cost: number | null, turns: number | null) =>
    [cost !== null ? format.money(cost) : null, turns !== null ? t("turns", { turns: format.decimal(turns) }) : null].filter(Boolean).join(", ");
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="ledger-replay" data-won={verdict.won}>
      <div className="flex flex-wrap items-center gap-2">
        <Tag className={verdict.won ? "bg-success-soft text-success" : undefined}>
          {verdict.won ? <Trophy aria-hidden="true" /> : <FlaskConical aria-hidden="true" />}
          {verdict.won ? t("won") : t("notWon")}
        </Tag>
        <span className="text-xs text-muted-foreground tabular-nums">
          {t("arms", {
            tasks: verdict.tasks,
            a: verdict.a.passed,
            b: verdict.b.passed,
          })}
        </span>
      </div>
      {mean(verdict.a.cost, verdict.a.turns) || mean(verdict.b.cost, verdict.b.turns) ? (
        <p className="text-xs text-muted-foreground tabular-nums">
          {t("means", { a: mean(verdict.a.cost, verdict.a.turns) || "-", b: mean(verdict.b.cost, verdict.b.turns) || "-" })}
        </p>
      ) : null}
    </div>
  );
}

export function LineRelease({ line }: { line: LedgerLine }) {
  const t = useTranslations("curator.ledger.release");
  if (line.action !== "skill_release") return null;
  const release = releaseOf(line.details);
  if (release === null) return null;
  const Icon = !release.published ? PackageX : release.kind === "republish" ? Undo2 : PackageCheck;
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid="ledger-release" data-kind={release.kind} data-published={release.published}>
      <div className="flex flex-wrap items-center gap-2">
        <Tag className={release.published ? "bg-success-soft text-success" : undefined}>
          <Icon aria-hidden="true" />
          {release.published ? t(release.kind === "republish" ? "republished" : "published") : t("nothing")}
        </Tag>
        <span className="font-mono text-xs text-foreground">{release.skill}</span>
        {release.version !== null ? <span className="text-xs text-muted-foreground tabular-nums">{t("version", { version: release.version })}</span> : null}
        {release.previous !== null && release.kind === "publish" && release.published ? (
          <span className="text-xs text-fg-subtle tabular-nums">{t("kept", { version: release.previous })}</span>
        ) : null}
        {release.reverted !== null && release.published ? (
          <span className="text-xs text-fg-subtle tabular-nums">{t("after", { version: release.reverted })}</span>
        ) : null}
      </div>
      {release.why ? <p className="text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{release.why}</p> : null}
    </div>
  );
}
