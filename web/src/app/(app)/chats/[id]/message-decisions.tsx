"use client";

import { DecisionCard } from "@/components/inbox/decision-view";
import type { Decision } from "@/components/inbox/queries";
import { decisionAnchor } from "@/components/runs/decision-queries";

/**
 * The decisions an agent's run asked, right under the message that ended its turn: the kit's DecisionCard (web/DESIGN.md,
 * Chats), the answer form for the run's owner with the agent's pick chosen, the question and options for anyone else,
 * and once answered the compact block with the answer.
 */
export function MessageDecisions({ decisions, onSettled }: { decisions: Decision[]; onSettled: (decision: Decision) => void }) {
  if (decisions.length === 0) return null;
  return (
    <div className="flex min-w-0 flex-col gap-2 sm:max-w-[46rem]" data-testid="message-decisions">
      {decisions.map((decision) => (
        <div key={decision.id} id={decisionAnchor(decision.id)} tabIndex={-1} className="scroll-mt-4 rounded-md" data-testid="chat-decision" data-decision-id={decision.id} data-state={decision.state}>
          <DecisionCard decision={decision} where="run" onAnswered={onSettled} onConflict={onSettled} />
        </div>
      ))}
    </div>
  );
}
