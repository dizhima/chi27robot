import type { ConversationMessage } from "./conversationTypes";
import type { StreamEvent } from "./conversationStreamClient";

export const APPLY_EDIT_CHAT_TEXT = "Apply edits";
export const RECHECK_PLAN_CHAT_TEXT = "Re-check plan";

export function createSyncChatMessages(
  messages: ConversationMessage[],
  ids: { userId: string; assistantId: string },
  hasPendingEdits: boolean,
): { requestMessages: ConversationMessage[]; visibleMessages: ConversationMessage[] } {
  const requestMessages = [
    ...messages,
    {
      id: ids.userId,
      role: "user" as const,
      content: hasPendingEdits ? APPLY_EDIT_CHAT_TEXT : RECHECK_PLAN_CHAT_TEXT,
    },
  ];
  const assistant: ConversationMessage = {
    id: ids.assistantId,
    role: "assistant",
    status: "working",
    intent: "resolve",
    content: hasPendingEdits ? "Applying your edits…" : "Re-checking conflicts…",
  };
  return { requestMessages, visibleMessages: [...requestMessages, assistant] };
}

export function applySyncProgress(
  messages: ConversationMessage[],
  assistantId: string,
  event: StreamEvent,
): ConversationMessage[] {
  if (
    event.text === undefined
    || (event.type !== "progress" && event.type !== "intent_selected" && event.type !== "warning")
  ) {
    return messages;
  }
  return messages.map((message) =>
    message.id === assistantId
      ? {
          ...message,
          content: event.text!,
          intent:
            event.type === "intent_selected" && event.intent
              ? (event.intent as ConversationMessage["intent"])
              : message.intent,
          activities: [
            ...(message.activities ?? []),
            { seq: event.seq ?? 0, stage: event.stage, text: event.text! },
          ],
        }
      : message,
  );
}
