"use client";

import { Plus } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

import { useAddSource, useMarketFailure } from "./data";
import { REPO_PATTERN, repoOf } from "./model";

/**
 * Add a market, for hub admins: a GitHub repo, the branch or tag and the path of its plugin-market-v1 index, and for a
 * private repo a token the hub seals and never shows again. The hub reads the index at once; a market whose index does
 * not read is added with its error, which the table shows until a refresh reads it.
 */
export function AddSourceDialog() {
  const t = useTranslations("market.admin.add");
  const id = useId();
  const add = useAddSource();
  const failure = useMarketFailure();
  const [open, setOpen] = useState(false);
  const [repo, setRepo] = useState("");
  const [ref, setRef] = useState("main");
  const [path, setPath] = useState("market.json");
  const [token, setToken] = useState("");
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const name = repoOf(repo);
  const invalid = repo.trim() !== "" && !REPO_PATTERN.test(name);

  const reset = () => {
    setRepo("");
    setRef("main");
    setPath("market.json");
    setToken("");
    setRefused(null);
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!REPO_PATTERN.test(name)) return;
    setRefused(null);
    try {
      const source = await add.mutateAsync({ repo: name, ref: ref.trim() || "main", path: path.trim() || "market.json", token: token || null });
      setToken("");
      notify({
        tone: source.error ? "info" : "success",
        text: t("added", { repo: source.repo }),
        description: source.error ? t("addedWithError", { error: source.error }) : t("addedCount", { count: source.plugins }),
      });
      setOpen(false);
      reset();
    } catch (error) {
      setRefused(failure(error));
    }
  };

  return (
    <>
      <Button onClick={() => setOpen(true)} data-testid="market-source-add">
        <Plus aria-hidden="true" />
        {t("open")}
      </Button>
      <Dialog
        open={open}
        onOpenChange={(next) => {
          if (!next && !add.isPending) {
            setOpen(false);
            reset();
          }
        }}
      >
        <DialogContent className="sm:max-w-md" data-testid="market-source-dialog">
          <DialogHeader>
            <DialogTitle>{t("title")}</DialogTitle>
            <DialogDescription>{t("description")}</DialogDescription>
          </DialogHeader>
          <form className="flex min-w-0 flex-col gap-4" onSubmit={(event) => void submit(event)} noValidate>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${id}-repo`}>{t("repo")}</Label>
              <Input
                id={`${id}-repo`}
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
                placeholder="northwind/market"
                autoComplete="off"
                spellCheck={false}
                required
                aria-invalid={invalid || undefined}
                aria-describedby={`${id}-repo-hint`}
                data-testid="market-source-repo"
              />
              <p id={`${id}-repo-hint`} className={invalid ? "text-xs text-danger" : "text-xs text-muted-foreground"}>
                {invalid ? t("repoInvalid") : t("repoHint")}
              </p>
            </div>
            <div className="grid min-w-0 gap-4 sm:grid-cols-2">
              <div className="flex min-w-0 flex-col gap-1.5">
                <Label htmlFor={`${id}-ref`}>{t("ref")}</Label>
                <Input id={`${id}-ref`} value={ref} onChange={(event) => setRef(event.target.value)} autoComplete="off" spellCheck={false} />
              </div>
              <div className="flex min-w-0 flex-col gap-1.5">
                <Label htmlFor={`${id}-path`}>{t("path")}</Label>
                <Input id={`${id}-path`} value={path} onChange={(event) => setPath(event.target.value)} autoComplete="off" spellCheck={false} />
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${id}-token`}>{t("token")}</Label>
              <Input
                id={`${id}-token`}
                type="password"
                value={token}
                onChange={(event) => setToken(event.target.value)}
                autoComplete="off"
                aria-describedby={`${id}-token-hint`}
              />
              <p id={`${id}-token-hint`} className="text-xs text-muted-foreground">
                {t("tokenHint")}
              </p>
            </div>
            {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
            <DialogFooter>
              <Button type="button" variant="outline" size="lg" onClick={() => setOpen(false)} disabled={add.isPending}>
                {t("cancel")}
              </Button>
              <Button type="submit" size="lg" busy={add.isPending} disabled={!REPO_PATTERN.test(name)} data-testid="market-source-save">
                {add.isPending ? t("adding") : t("save")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}
