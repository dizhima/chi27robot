import type { CSSProperties } from "react";
import { ganttColorsForRobot } from "../robotVisuals";
import type { ConversationMessage } from "./conversationTypes";

type UserMessageProps = {
  message: ConversationMessage;
};

export function UserMessage({ message }: UserMessageProps) {
  return (
    <article className="chat-rail-message is-user" aria-label="Your message">
      <p>
        {message.displayParts?.length
          ? message.displayParts.map((part, index) => {
              if (part.type === "text") return part.value;
              const legacyRobot = part.kind === "plan_task"
                ? /^(robot\d+)\s*·/.exec(part.label)?.[1]
                : undefined;
              const robot = part.robot ?? legacyRobot;
              const colors = robot ? ganttColorsForRobot(robot) : null;
              return (
                <span
                  className={`ref-token is-${part.kind} message-ref-token`}
                  key={`${part.kind}-${part.label}-${index}`}
                  style={colors ? {
                    "--robot-bar-background": colors.background,
                    "--robot-bar-border": colors.border,
                  } as CSSProperties : undefined}
                >
                  {part.kind === "position" ? "📍 " : part.kind === "robot" ? "● " : part.kind === "facility" ? "◇ " : part.kind === "plan_task" ? "▭ " : ""}
                  {part.label}
                </span>
              );
            })
          : message.content}
      </p>
    </article>
  );
}
