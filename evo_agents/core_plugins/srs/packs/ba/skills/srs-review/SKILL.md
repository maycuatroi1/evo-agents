---
name: srs-review
description: Review a version of an SRS document of the hub against its template and look for contradictions between use cases, with the glossary and with the non-functional requirements, writing each finding as a comment with srs__comment.
---

# Reviewing a version

1. Read the version whole with `srs__read` (the document and the version's number). When a version was approved
   before, read what changed with `srs__diff` from that version to this one, and review the changed items first.
2. Check the template, item by item:
   - every heading and code the template asks for is there, and each code matches its pattern;
   - each use case has an actor, preconditions, numbered main steps, alternative flows where a step can fail, and
     postconditions;
   - each business rule names steps its use case has;
   - each screen a use case names exists, and each screen is used by a use case;
   - each requirement is measurable: a number, a limit, a time.
3. Look for contradictions:
   - **between use cases**: two use cases that say different things about the same object, state or limit (an order
     cancelled before payment in one, after shipping in another);
   - **with the glossary**: a term used with another meaning than the glossary gives, or a term the glossary lacks;
   - **with the non-functional requirements**: a step that breaks a requirement (a step that waits a minute where a
     requirement asks for two seconds, a field kept that a privacy requirement forbids).
4. Write each finding as one comment with `srs__comment`: the document, the version, and the item's code (or key)
   it is about. Start it with `Blocking:` for what must change before approval, or `Suggestion:` for the rest. Say
   what is wrong, where (quote at most a line), and what would fix it. A finding about the whole document goes on
   the version without an item.

Write at most 20 comments in one review, the blocking ones first. When you find nothing, write one comment on the
version that says what you checked.

## What a review never does

Your comments are advice. You do not approve or return the version, propose a version, create a task, or change any
file. In a chat thread, the member decides what to change; in a review run, the members of the project do.
