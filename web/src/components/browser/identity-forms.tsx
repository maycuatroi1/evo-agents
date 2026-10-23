"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { InlineError, type WriteFailure } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import { browserWorkers, IDENTITY_NAME, parseDomains, validLoginUrl } from "./model";
import { type BrowserIdentityCreate, MAX_URL_CHARS, workersQuery } from "./queries";

/** The worker field: the visitor's own workers first, then the shared ones, only those with the feature browser. */
function WorkerField({ project, value, onChange, exclude, id, invalid }: { project: string; value: string; onChange: (name: string) => void; exclude?: string; id: string; invalid?: boolean }) {
  const t = useTranslations("browser.form");
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const { data: all, isPending } = useQuery(workersQuery(browserApi));
  const login = me?.login ?? null;
  const workers = browserWorkers(all ?? [], project, login).filter((worker) => worker.name !== exclude);
  const own = workers.filter((worker) => worker.owner === login);
  const shared = workers.filter((worker) => worker.owner !== login);
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>{t("worker")}</Label>
      <NativeSelect id={id} className="w-full" value={value} onChange={(event) => onChange(event.target.value)} aria-invalid={invalid || undefined} aria-describedby={`${id}-hint`} data-testid="browser-worker">
        <NativeSelectOption value="">{isPending ? t("workersLoading") : t("workerPick")}</NativeSelectOption>
        {own.length ? (
          <optgroup label={t("ownWorkers")}>
            {own.map((worker) => (
              <NativeSelectOption key={worker.id} value={worker.name}>
                {worker.name}
              </NativeSelectOption>
            ))}
          </optgroup>
        ) : null}
        {shared.length ? (
          <optgroup label={t("sharedWorkers")}>
            {shared.map((worker) => (
              <NativeSelectOption key={worker.id} value={worker.name}>
                {t("sharedWorker", { name: worker.name, owner: worker.owner })}
              </NativeSelectOption>
            ))}
          </optgroup>
        ) : null}
      </NativeSelect>
      <p id={`${id}-hint`} className="text-xs text-muted-foreground">
        {!isPending && workers.length === 0 ? t("noWorkers") : t("workerHint")}
      </p>
    </div>
  );
}

type Errors = Partial<Record<"name" | "domains" | "loginUrl" | "worker", string>>;

/** New: a name, the domains whose cookies it keeps, the login URL and the worker that holds the profile. */
export function NewIdentityDialog({
  project,
  open,
  onOpenChange,
  pending,
  error,
  canProject,
  onCreate,
}: {
  project: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  error: WriteFailure | null;
  canProject: boolean;
  onCreate: (body: BrowserIdentityCreate) => void;
}) {
  const t = useTranslations("browser.form");
  const ids = useId();
  const [name, setName] = useState("");
  const [domains, setDomains] = useState("");
  const [loginUrl, setLoginUrl] = useState("");
  const [worker, setWorker] = useState("");
  const [owner, setOwner] = useState<"member" | "project">("member");
  const [errors, setErrors] = useState<Errors>({});

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const list = parseDomains(domains);
    const found: Errors = {};
    if (!IDENTITY_NAME.test(name.trim())) found.name = t("nameInvalid");
    if (list.length === 0) found.domains = t("domainsRequired");
    if (loginUrl.trim() && (!validLoginUrl(loginUrl.trim()) || loginUrl.trim().length > MAX_URL_CHARS)) found.loginUrl = t("loginUrlInvalid");
    if (!worker) found.worker = t("workerRequired");
    setErrors(found);
    if (Object.keys(found).length) return;
    onCreate({ name: name.trim(), domains: list, login_url: loginUrl.trim() || null, worker, owner });
  };

  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-lg" data-testid="browser-new-dialog">
        <DialogHeader>
          <DialogTitle>{t("newTitle")}</DialogTitle>
          <DialogDescription>{t("newDescription")}</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} noValidate className="flex flex-col gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-name`}>{t("name")}</Label>
            <Input id={`${ids}-name`} value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} aria-invalid={errors.name ? true : undefined} aria-describedby={`${ids}-name-note`} className="max-md:text-base" data-testid="browser-name" />
            <p id={`${ids}-name-note`} className={errors.name ? "text-xs text-danger" : "text-xs text-muted-foreground"}>
              {errors.name ?? t("nameHint")}
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-domains`}>{t("domains")}</Label>
            <Input id={`${ids}-domains`} value={domains} onChange={(event) => setDomains(event.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} aria-invalid={errors.domains ? true : undefined} aria-describedby={`${ids}-domains-note`} className="max-md:text-base" placeholder="github.com, gist.github.com" data-testid="browser-domains" />
            <p id={`${ids}-domains-note`} className={errors.domains ? "text-xs text-danger" : "text-xs text-muted-foreground"}>
              {errors.domains ?? t("domainsHint")}
            </p>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-url`}>{t("loginUrl")}</Label>
            <Input id={`${ids}-url`} type="url" inputMode="url" value={loginUrl} onChange={(event) => setLoginUrl(event.target.value)} autoComplete="off" autoCapitalize="off" spellCheck={false} aria-invalid={errors.loginUrl ? true : undefined} aria-describedby={`${ids}-url-note`} className="max-md:text-base" placeholder="https://github.com/login" data-testid="browser-login-url" />
            <p id={`${ids}-url-note`} className={errors.loginUrl ? "text-xs text-danger" : "text-xs text-muted-foreground"}>
              {errors.loginUrl ?? t("loginUrlHint")}
            </p>
          </div>
          <WorkerField project={project} value={worker} onChange={setWorker} id={`${ids}-worker`} invalid={Boolean(errors.worker)} />
          {errors.worker ? <p className="-mt-2 text-xs text-danger">{errors.worker}</p> : null}
          {canProject ? (
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={`${ids}-owner`}>{t("owner")}</Label>
              <NativeSelect id={`${ids}-owner`} className="w-full" value={owner} onChange={(event) => setOwner(event.target.value === "project" ? "project" : "member")} data-testid="browser-owner">
                <NativeSelectOption value="member">{t("ownerMember")}</NativeSelectOption>
                <NativeSelectOption value="project">{t("ownerProject")}</NativeSelectOption>
              </NativeSelect>
            </div>
          ) : null}
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={pending}>
                {t("cancel")}
              </Button>
            </DialogClose>
            <Button type="submit" busy={pending} data-testid="browser-create">
              {pending ? t("creating") : t("create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** Move: a login session on another worker; the identity moves there once the session is saved. */
export function MoveDialog({
  project,
  name,
  current,
  open,
  onOpenChange,
  pending,
  error,
  onMove,
}: {
  project: string;
  name: string;
  current: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pending: boolean;
  error: WriteFailure | null;
  onMove: (worker: string) => void;
}) {
  const t = useTranslations("browser.move");
  const ids = useId();
  const [worker, setWorker] = useState("");
  const [missing, setMissing] = useState(false);
  return (
    <Dialog open={open} onOpenChange={(next) => (pending ? undefined : onOpenChange(next))}>
      <DialogContent className="flex max-h-[calc(100dvh-2rem)] flex-col gap-5 overflow-y-auto sm:max-w-md" data-testid="browser-move-dialog">
        <DialogHeader>
          <DialogTitle>{t("title", { name })}</DialogTitle>
          <DialogDescription>{t("description", { worker: current })}</DialogDescription>
        </DialogHeader>
        <form
          noValidate
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            setMissing(!worker);
            if (worker) onMove(worker);
          }}
        >
          <WorkerField project={project} value={worker} onChange={setWorker} exclude={current} id={`${ids}-worker`} invalid={missing} />
          {missing ? <p className="-mt-2 text-xs text-danger">{t("required")}</p> : null}
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline" disabled={pending}>
                {t("cancel")}
              </Button>
            </DialogClose>
            <Button type="submit" busy={pending} data-testid="browser-move-start">
              {pending ? t("starting") : t("start")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
