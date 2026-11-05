import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { ACTIVE_REFRESH } from "./queries";

/**
 * The reads of an answer job's page (`evo-agents hub kg answer`): the job and its counts (GET
 * /v1/kg/{project}/answers/{id}), asked again while it runs, and its answers in the file's order (GET
 * .../{id}/lines, ANSWER_PAGE lines a read), which the page joins into answers.jsonl as the CLI writes it.
 */
export type KgAnswerJob = components["schemas"]["KgAnswerJob"];
export type KgAnswerLine = components["schemas"]["KgAnswerLine"];

export const ANSWER_PAGE = 1000;
const LINE_KEYS = ["id", "question", "answer", "evidence", "model", "input_tokens", "output_tokens"] as const;

export const answerKeys = {
  job: (project: string, id: number) => ["kg", project, "answers", id] as const,
};

export const kgAnswerJobQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: answerKeys.job(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/answers/{job_id}", { params: { path: { project, job_id: id } }, signal })),
    refetchInterval: (query) => (!query.state.data || query.state.data.state === "running" ? ACTIVE_REFRESH : false),
  });

/** Every answer of job `id`, read page by page. */
export async function answerLines(api: ApiSource, project: string, id: number): Promise<KgAnswerLine[]> {
  const out: KgAnswerLine[] = [];
  for (;;) {
    const found = await call(
      api().GET("/v1/kg/{project}/answers/{job_id}/lines", {
        params: { path: { project, job_id: id }, query: { offset: out.length, limit: ANSWER_PAGE } },
      }),
    );
    out.push(...found.lines);
    if (found.lines.length === 0 || out.length >= found.total) return out;
  }
}

/** answers.jsonl: one JSON object a line, the CLI's keys in its order, `error` only on a question with no answer. */
export function answersFile(lines: KgAnswerLine[]): string {
  return lines
    .map((line) => {
      const kept: Record<string, unknown> = Object.fromEntries(LINE_KEYS.map((key) => [key, line[key]]));
      if (line.error) kept.error = line.error;
      return `${JSON.stringify(kept)}\n`;
    })
    .join("");
}

/** The share of the job's questions that have ended, answered or not, 0 to 100. */
export function answeredShare(job: KgAnswerJob): number {
  return job.questions > 0 ? ((job.answered + job.errors) / job.questions) * 100 : 0;
}
