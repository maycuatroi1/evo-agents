"use client";

import { cn } from "@/lib/utils";

import type { Run, RunEvent } from "./queries";
import { RunAccounts } from "./run-accounts";
import { RunCredentials } from "./run-credentials";
import { RunDetails, RunResult } from "./run-facts";
import type { RunViewer } from "./run-model";
import { RunPlanSteps } from "./run-plan-steps";
import { RunSessions } from "./run-sessions";
import { UsageMeter } from "./usage-meter";

/**
 * A phone's side cards as flat sections of the page: no frame, shadow or inner gutter, so their titles and rows share
 * the page's left edge, a hairline and 20 px between one section and the next.
 */
const FLAT_SECTIONS =
  "gap-0 [&>section]:rounded-none [&>section]:border-0 [&>section]:bg-transparent [&>section]:shadow-none [&>section>div]:border-0 [&>section>div]:px-0 [&>section>div:first-child]:pt-0 [&>section>div:first-child]:pb-1 [&>section+section]:border-t [&>section+section]:pt-5 [&>section]:pb-5 [&_[data-steps]]:-mx-2";

/**
 * What the Details toggle opens beside a run's trace (under it below xl): the plan's steps, the essential details (the
 * rest, with the owner's accounts and credentials, behind "Show all details"), then usage and the result once there are
 * any. `flat` on a phone, where they are sections of the page.
 */
export function RunSide({ run, viewer, owner, events, flat }: { run: Run; viewer: RunViewer | null; owner: boolean; events: RunEvent[]; flat: boolean }) {
  const plan = run.kind === "plan";
  return (
    <div className={cn("relative grid min-w-0 items-start gap-4 md:grid-cols-2 xl:grid-cols-1", flat && FLAT_SECTIONS)} data-testid="run-side">
      {plan ? <RunPlanSteps run={run} /> : null}
      <RunDetails
        run={run}
        viewer={viewer}
        more={
          owner ? (
            <>
              <RunAccounts run={run} />
              <RunCredentials run={run} />
            </>
          ) : null
        }
      />
      <UsageMeter run={run} events={events} />
      {plan && run.session_per_step ? <RunSessions run={run} /> : null}
      <RunResult run={run} />
    </div>
  );
}
