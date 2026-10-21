/**
 * Where a step's report starts in an agent's message: a line `step: <id>` then a line `result: pass` or `result: fail`
 * (step-report.ts reads the rest). Apart from the parser, so the Trace tells a message that holds a report without
 * loading the parser and the card (trace-report-lazy.tsx).
 */

export const START = /^([ \t]*)step:[ \t]*\S/;
const RESULT = /^[ \t]*result:[ \t]*(pass|fail)\b/i;

/** The message's lines, any line break as one. */
export const linesOf = (text: string): string[] => text.replace(/\r\n?/g, "\n").split("\n");

/** The index of the line the report starts at, or -1 when there is none. */
export function reportStart(lines: string[]): number {
  return lines.findIndex((line, index) => START.test(line) && RESULT.test(lines[index + 1] ?? ""));
}

/** Whether `text` holds a report; parseStepReport reads it only then. */
export const holdsStepReport = (text: string): boolean => reportStart(linesOf(text)) >= 0;
