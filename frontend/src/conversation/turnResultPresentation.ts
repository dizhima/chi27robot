import type { TurnOutcome } from "./conversationTypes";

type TurnResult = Extract<TurnOutcome, { kind: "turn_result" }>;

/** Main assistant bubble. Detailed compiler/resolver adjustments never leak here. */
export function turnResultBubbleContent(outcome: TurnResult): string {
  if (outcome.authorMessage) {
    return outcome.resolveWarning
      ? `${outcome.authorMessage}\n\n${outcome.resolveWarning}`
      : outcome.authorMessage;
  }
  if (outcome.editMessage) {
    return outcome.resolveWarning
      ? `${outcome.editMessage}\n\n${outcome.resolveWarning}`
      : outcome.editMessage;
  }
  return outcome.resolveWarning
    ? `Re-checked the plan.\n\n${outcome.resolveWarning}`
    : "Re-checked the plan.";
}

/** Accountability/audit text shown only after expanding See details. */
export function turnResultDetails(outcome: TurnResult): string | undefined {
  const sections = [outcome.authoringSummary, outcome.resolveSummary].filter(
    (section): section is string => Boolean(section),
  );
  return sections.length ? sections.join("\n") : undefined;
}
