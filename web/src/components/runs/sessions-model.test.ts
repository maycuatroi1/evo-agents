import { describe, expect, it } from "vitest";

import { readStepSessions } from "./sessions-model";

describe("readStepSessions", () => {
  it("reads each step's session from the run's usage", () => {
    const usage = {
      input_tokens: 17,
      peak_context_tokens: 900,
      sessions: [
        { step: "1", session_id: "s1", run_id: 7, peak_context_tokens: 900, cache_read_input_tokens: 100 },
        { step: 2, session_id: null, run_id: 8, peak_context_tokens: null, cached_input_tokens: 40 },
        { step: "3", session_id: "s3", run_id: 8, cache_read: 5 },
        { session_id: "no step" },
        "junk",
      ],
      sessions_left_out: 4,
    };
    expect(readStepSessions(usage)).toEqual({
      sessions: [
        { step: "1", sessionId: "s1", runId: 7, peak: 900, cacheRead: 100 },
        { step: "2", sessionId: null, runId: 8, peak: null, cacheRead: 40 },
        { step: "3", sessionId: "s3", runId: 8, peak: null, cacheRead: 5 },
      ],
      leftOut: 4,
    });
  });

  it("is null for usage that lists no sessions", () => {
    expect(readStepSessions(null)).toBeNull();
    expect(readStepSessions({ input_tokens: 3 })).toBeNull();
    expect(readStepSessions({ sessions: "x" })).toBeNull();
    expect(readStepSessions({ sessions: [] })).toEqual({ sessions: [], leftOut: 0 });
  });
});
