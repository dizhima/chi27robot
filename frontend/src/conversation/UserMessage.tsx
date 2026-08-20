import type { ConversationMessage } from "./conversationTypes";

type UserMessageProps = {
  message: ConversationMessage;
};

export function UserMessage({ message }: UserMessageProps) {
  return (
    <article className="chat-rail-message is-user">
      <strong>You</strong>
      <p>
        {message.displayParts?.length
          ? message.displayParts.map((part, index) =>
              part.type === "text" ? (
                part.value
              ) : (
                <span
                  className={`ref-token is-${part.kind} message-ref-token`}
                  key={`${part.kind}-${part.label}-${index}`}
                >
                  {part.kind === "position" ? "📍 " : part.kind === "facility" ? "◇ " : part.kind === "plan_task" ? "▭ " : ""}
                  {part.label}
                </span>
              ),
            )
          : message.content}
      </p>
    </article>
  );
}
