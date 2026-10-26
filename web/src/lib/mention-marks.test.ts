import { describe, expect, it } from "vitest";

import { chatMentionMarks, type MentionMark, splitMentions } from "./mention-marks";

const marks = chatMentionMarks({
  agents: [
    { handle: "evo-lms", name: "evo-lms" },
    { handle: "reviewer", name: "Code reviewer" },
  ],
  people: [{ login: "maycuatroi1" }, { login: "canhwindy" }, { login: "an" }],
});
const named = (pieces: ReturnType<typeof splitMentions>) => pieces.map((piece) => (typeof piece === "string" ? piece : `[${piece.label}]`)).join("");

describe("splitMentions", () => {
  it("marks an agent after @ and a person after @ or bare, by name whatever its case", () => {
    expect(named(splitMentions("@evo-lms làm luôn cho tôi đi", marks))).toBe("[evo-lms] làm luôn cho tôi đi");
    expect(named(splitMentions("Chào anh MayCuaTroi1, do bachtha và canhwindy yêu cầu.", marks))).toBe("Chào anh [maycuatroi1], do bachtha và [canhwindy] yêu cầu.");
    expect(named(splitMentions("@Reviewer và @canhwindy", marks))).toBe("[Code reviewer] và [canhwindy]");
  });

  it("leaves a name that is part of a longer one, a path or an address, and a short login written bare", () => {
    const text = "repo maycuatroi1/evo-lms-landing-page, @evo-lms-landing-page, a@evo-lms, maycuatroi1.github.io, an toàn, @an";
    expect(named(splitMentions(text, marks))).toBe("repo maycuatroi1/evo-lms-landing-page, @evo-lms-landing-page, a@evo-lms, maycuatroi1.github.io, an toàn, [an]");
  });

  it("keeps an agent's handle written bare as text: it is often a project's name too", () => {
    expect(splitMentions("project evo-lms trên hub", marks)).toEqual(["project evo-lms trên hub"]);
  });

  it("gives the text back whole when nobody is named", () => {
    const none: MentionMark[] = [];
    expect(splitMentions("@evo-lms", none)).toEqual(["@evo-lms"]);
    expect(splitMentions("", marks)).toEqual([]);
  });
});
