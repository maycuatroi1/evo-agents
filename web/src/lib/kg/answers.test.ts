import { describe, expect, it } from "vitest";

import { answeredShare, answersFile, type KgAnswerJob, type KgAnswerLine } from "./answer-queries";
import { answersHref } from "./routes";

const line = (id: string, error: string | null = null): KgAnswerLine => ({
  id,
  question: `what is ${id}?`,
  answer: error ? null : `${id} is a thing`,
  evidence: error ? [] : [`repo:${id}.md`],
  model: error ? null : "claude-sonnet-5-5",
  input_tokens: error ? 0 : 10,
  output_tokens: error ? 0 : 2,
  error,
});

describe("answer jobs", () => {
  it("writes answers.jsonl as the CLI does: its keys in order, error only on a question with no answer", () => {
    const text = answersFile([line("q-1"), line("q-2", "run #3 ended cancelled without an answer to this question")]);
    const rows = text.trimEnd().split("\n").map((row) => JSON.parse(row));
    expect(text.endsWith("\n")).toBe(true);
    expect(Object.keys(rows[0])).toEqual(["id", "question", "answer", "evidence", "model", "input_tokens", "output_tokens"]);
    expect(rows[1].error).toContain("run #3");
    expect(rows[1].answer).toBeNull();
  });

  it("counts the questions ended, answered or not, and links the job's page", () => {
    expect(answeredShare({ questions: 4, answered: 1, errors: 1 } as KgAnswerJob)).toBe(50);
    expect(answeredShare({ questions: 0, answered: 0, errors: 0 } as KgAnswerJob)).toBe(0);
    expect(answersHref("acme", 7)).toBe("/p/acme/kg/answers/7");
  });
});
