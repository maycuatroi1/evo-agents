import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";

/**
 * What the plans list writes, through the hub's own endpoints for one plan at a time: Abandon (POST .../abandon, with
 * the reason) and Close (POST .../complete, for a plan whose steps are all done or descoped). A write names the
 * revision the list showed, so a plan that changed since is refused (409) rather than closed blind.
 */

export type PlanAction = "abandon" | "close";

/** A plan a write goes to: its id, the name the list shows, and the revision it showed. */
export type PlanTarget = { plan_id: string; name: string; revision: number };

/** A plan the write did not reach, with the hub's words (or the network's) for why. */
export type PlanFailure = { plan_id: string; name: string; status: number; message: string };

export type PlanWriteResult = { done: PlanTarget[]; failed: PlanFailure[] };

export async function writePlan(api: ApiClient, project: string, action: PlanAction, plan: PlanTarget, reason: string): Promise<void> {
  const headers = await csrfHeaders(api);
  const params = { path: { project, plan_id: plan.plan_id } };
  if (action === "abandon") {
    await call(
      api.POST("/v1/projects/{project}/plans/{plan_id}/abandon", { params, body: { reason, if_revision: plan.revision }, headers }),
    );
    return;
  }
  await call(api.POST("/v1/projects/{project}/plans/{plan_id}/complete", { params, body: { if_revision: plan.revision, mark_steps_done: false }, headers }));
}

/**
 * Write each plan in turn (one request at a time, so the hub's audit reads in the list's order) and keep going past a
 * refusal: every plan comes back either done or failed with its reason.
 */
export async function writeEach(
  plans: readonly PlanTarget[],
  write: (plan: PlanTarget) => Promise<void>,
): Promise<PlanWriteResult> {
  const result: PlanWriteResult = { done: [], failed: [] };
  for (const plan of plans) {
    try {
      await write(plan);
      result.done.push(plan);
    } catch (error) {
      const info = isApiError(error) ? error.info : { status: 0, message: String(error) };
      result.failed.push({ plan_id: plan.plan_id, name: plan.name, status: info.status, message: info.message });
    }
  }
  return result;
}
