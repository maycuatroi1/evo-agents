"use client";

import { useQuery } from "@tanstack/react-query";
import { FolderPlus, Building2, OctagonX, UserPlus } from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import type { WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { CeilingDialog, type CeilingTarget } from "./ceiling-dialog";
import { type OrgAction, type OrgView, OrgViewContext } from "./context";
import { useOrgFailure, useOrgWrite, useToday } from "./hooks";
import { buildTree, knownLogins, rightsOf, rootOf, runsByLogin, unplaced } from "./model";
import { type OrgChart, type OrgUser, orgAutonomyQuery, orgQuery, orgRunsQuery, orgUsersQuery, type StopSet } from "./queries";
import { SeatDialog, type SeatTarget } from "./seat-dialog";
import { StopDialog, StopFlag } from "./stop-parts";
import { ALL, heldStop, stopRightOf } from "./stops";
import { UnitDialog, type UnitTarget } from "./unit-dialog";
import { UnitBranch } from "./unit-tree";

type Dialogs = { unit: UnitTarget | null; seat: SeatTarget | null; ceiling: CeilingTarget | null; confirm: OrgAction | null; stop: StopSet | null };
type Open = { unit: boolean; seat: boolean; ceiling: boolean; confirm: boolean; stop: boolean };
const CLOSED: Open = { unit: false, seat: false, ceiling: false, confirm: false, stop: false };

/** Ends a seat or archives a unit, after asking; the failure stays in the confirmation. */
function Confirm({ action, open, onOpenChange }: { action: OrgAction | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("org.confirm");
  const write = useOrgWrite();
  const failure = useOrgFailure();
  const [error, setError] = useState<WriteFailure | null>(null);
  if (!action || (action.kind !== "endSeat" && action.kind !== "archiveUnit")) return null;
  const seat = action.kind === "endSeat" ? action.seat : null;
  const name = seat ? (seat.login ?? seat.agent ?? "") : action.kind === "archiveUnit" ? action.unit.name : "";
  const confirm = async () => {
    setError(null);
    try {
      await write.mutateAsync(seat ? { kind: "endSeat", id: seat.id } : { kind: "archiveUnit", slug: action.kind === "archiveUnit" ? action.unit.slug : "" });
    } catch (caught) {
      setError(failure(caught));
      return;
    }
    notify({ tone: "success", text: seat ? t("seatEnded", { name }) : t("unitArchived", { name }) });
    onOpenChange(false);
  };
  return (
    <ConfirmAction
      open={open}
      onOpenChange={(next) => {
        if (!next) setError(null);
        onOpenChange(next);
      }}
      title={seat ? t("endSeatTitle", { name }) : t("archiveTitle", { name })}
      description={<p>{seat ? t("endSeatText") : t("archiveText")}</p>}
      confirmLabel={seat ? t("endSeat") : t("archive")}
      pendingLabel={seat ? t("ending") : t("archiving")}
      cancelLabel={t("cancel")}
      pending={write.isPending}
      error={error}
      onConfirm={() => void confirm()}
      testId="org-confirm"
    />
  );
}

/** The people with a grant who hold no seat outside the root, each with a way to place them; a hub admin's list. */
function Unplaced({ people, onPlace }: { people: OrgUser[]; onPlace: (login: string) => void }) {
  const t = useTranslations("org.unplaced");
  return (
    <section aria-labelledby="org-unplaced-title" className="flex min-w-0 flex-col gap-2" data-testid="org-unplaced">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <h2 id="org-unplaced-title" className="text-sm font-semibold">
          {t("title")}
        </h2>
        <span className="text-xs text-muted-foreground">{t("summary", { count: people.length })}</span>
      </div>
      {people.length === 0 ? (
        <p className="text-xs text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="flex min-w-0 flex-col divide-y divide-border rounded-md border bg-card">
          {people.map((person) => (
            <li key={person.login} className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2" data-testid="org-unplaced-person" data-login={person.login}>
              <span className="min-w-0 flex-1 font-mono text-[13px] [overflow-wrap:anywhere]">{person.login}</span>
              <span className="text-xs text-muted-foreground">{t("grants", { count: person.grants.length })}</span>
              <Button type="button" variant="outline" size="sm" onClick={() => onPlace(person.login)} aria-label={t("placeLabel", { login: person.login })} data-testid="org-unplaced-place">
                <UserPlus aria-hidden="true" />
                {t("place")}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function OrgChartView({ chart, all, act }: { chart: OrgChart; all: boolean; act: (action: OrgAction) => void }) {
  const t = useTranslations("org");
  const today = useToday();
  const runs = useQuery(orgRunsQuery(browserApi));
  const users = useQuery({ ...orgUsersQuery(browserApi), enabled: chart.viewer.admin });
  const autonomy = useQuery(orgAutonomyQuery(browserApi));
  const rights = useMemo(() => rightsOf(chart.viewer, chart.units), [chart]);
  const mayStop = useMemo(() => stopRightOf(chart, today ?? "0000-00-00"), [chart, today]);
  const tree = useMemo(() => buildTree(chart, today ?? "0000-00-00", all), [chart, today, all]);
  const view: OrgView = useMemo(
    () => ({ units: chart.units, rights, runs: runsByLogin(runs.data), today, act, autonomy: autonomy.data, mayStop }),
    [chart.units, rights, runs.data, today, act, autonomy.data, mayStop],
  );
  const stopAll = heldStop(autonomy.data, ALL);
  const people = users.data && today ? unplaced(users.data, chart, today) : null;
  const root = rootOf(chart.units);
  const placeIn = chart.units.find((unit) => unit.parent !== null && !unit.archived_at)?.slug ?? root?.slug ?? "";

  if (!tree || (tree.seats.length === 0 && tree.children.length === 0)) {
    return (
      <EmptyState icon={Building2} title={t("empty.title")} description={t("empty.description")} testId="org-empty">
        {root && rights.createUnder(root) ? (
          <Button onClick={() => act({ kind: "createUnit", parent: root })} data-testid="org-empty-create">
            <FolderPlus aria-hidden="true" />
            {t("newUnit")}
          </Button>
        ) : null}
      </EmptyState>
    );
  }
  return (
    <OrgViewContext.Provider value={view}>
      <div className="flex min-w-0 flex-col gap-6">
        {stopAll ? (
          <div className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-danger/40 bg-danger-soft px-3 py-2 text-sm" role="status" data-testid="org-stop-all-held">
            <span className="min-w-0 flex-1 font-medium">{t("stops.allHeld")}</span>
            <StopFlag stop={stopAll} testId="org-all-stopped" />
          </div>
        ) : null}
        <section aria-labelledby="org-tree-title" className="min-w-0">
          <h2 id="org-tree-title" className="sr-only">
            {t("treeTitle")}
          </h2>
          <ul className="flex min-w-0 flex-col rounded-lg border bg-surface-sunken/40 p-1 sm:p-2" data-testid="org-tree">
            <UnitBranch node={tree} />
          </ul>
        </section>
        {people ? <Unplaced people={people} onPlace={(login) => act({ kind: "addSeat", unit: placeIn, login })} /> : null}
      </div>
    </OrgViewContext.Provider>
  );
}

/**
 * The org chart: every unit from the root down with its seats, the level each one works at and from where, and each
 * person's runs at work over the projects the viewer may see. A hub admin or a unit's lead changes it from the rows'
 * menus; the hub decides each write and its reason shows in the dialog when it refuses.
 */
export function OrgPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("org");
  const state = useHubQuery(orgQuery(browserApi), initialError);
  const chart = state.status === "success" ? state.data : null;
  const [all, setAll] = useState(false);
  const [dialogs, setDialogs] = useState<Dialogs>({ unit: null, seat: null, ceiling: null, confirm: null, stop: null });
  const [open, setOpen] = useState<Open>(CLOSED);
  const [opened, setOpened] = useState(0);
  const rights = useMemo(() => (chart ? rightsOf(chart.viewer, chart.units) : null), [chart]);
  const root = chart ? rootOf(chart.units) : undefined;
  const users = useQuery({ ...orgUsersQuery(browserApi), enabled: chart?.viewer.admin === true });
  const stops = useQuery({ ...orgAutonomyQuery(browserApi), enabled: chart?.viewer.admin === true });
  const logins = useMemo(() => (chart ? knownLogins(chart, users.data) : []), [chart, users.data]);

  const act = useMemo(
    () => (action: OrgAction) => {
      const show = (key: keyof Dialogs, value: Dialogs[keyof Dialogs]) => {
        setDialogs((current) => ({ ...current, [key]: value }));
        setOpen({ ...CLOSED, [key]: true });
        setOpened((count) => count + 1);
      };
      if (action.kind === "createUnit") show("unit", { parent: action.parent });
      else if (action.kind === "editUnit") show("unit", { unit: action.unit });
      else if (action.kind === "unitCeiling") show("ceiling", { unit: action.unit });
      else if (action.kind === "seatCeiling") show("ceiling", { seat: action.seat });
      else if (action.kind === "addSeat") show("seat", { unit: action.unit, login: action.login });
      else if (action.kind === "editSeat") show("seat", { unit: action.seat.unit, seat: action.seat });
      else if (action.kind === "stop") show("stop", action.target);
      else show("confirm", action);
    },
    [],
  );
  const toggle = (key: keyof Open) => (next: boolean) => setOpen((current) => ({ ...current, [key]: next }));

  return (
    <>
      <PageHeader
        title={t("title")}
        testId="org-header"
        tags={chart ? <Badge variant="secondary" data-testid="org-viewer-level">{t("yourLevel", { level: chart.viewer.effective })}</Badge> : null}
        actions={
          <>
            <div className="flex items-center gap-2">
              <Switch id="org-show-all" checked={all} onCheckedChange={setAll} data-testid="org-show-all" />
              <Label htmlFor="org-show-all" className="text-[13px] font-normal">
                {t("showAll")}
              </Label>
            </div>
            {chart?.viewer.admin && stops.data && !heldStop(stops.data, ALL) ? (
              <Button variant="outline" onClick={() => act({ kind: "stop", target: ALL })} data-testid="org-stop-all">
                <OctagonX aria-hidden="true" />
                {t("stops.stopAll")}
              </Button>
            ) : null}
            {root && rights?.createUnder(root) ? (
              <Button onClick={() => act({ kind: "createUnit", parent: root })} data-testid="org-new-unit">
                <FolderPlus aria-hidden="true" />
                {t("newUnit")}
              </Button>
            ) : null}
          </>
        }
        sub={chart ? <span data-testid="org-summary">{t("summary", { units: chart.units.filter((unit) => !unit.archived_at).length, seats: chart.seats.filter((seat) => !seat.ended_at).length })}</span> : null}
      />
      <QueryView state={state} loading={<ListSkeleton rows={6} />}>
        {(data) => <OrgChartView chart={data} all={all} act={act} />}
      </QueryView>
      {chart && rights ? (
        <>
          <UnitDialog key={opened} target={dialogs.unit} open={open.unit} onOpenChange={toggle("unit")} units={chart.units} rights={rights} />
          <SeatDialog key={opened} target={dialogs.seat} open={open.seat} onOpenChange={toggle("seat")} units={chart.units} rights={rights} logins={logins} />
          <CeilingDialog key={opened} target={dialogs.ceiling} open={open.ceiling} onOpenChange={toggle("ceiling")} rights={rights} />
          <Confirm key={opened} action={dialogs.confirm} open={open.confirm} onOpenChange={toggle("confirm")} />
          <StopDialog key={`stop-${opened}`} target={dialogs.stop} units={chart.units} open={open.stop} onOpenChange={toggle("stop")} />
        </>
      ) : null}
    </>
  );
}
