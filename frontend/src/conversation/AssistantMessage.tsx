/**
 * Phase 4 Part E: renders one assistant ConversationMessage, including a
 * collapsible "See details" toggle above the final answer.
 *
 * While the turn is still working, behavior is unchanged from Phase 2/3: the
 * headline shows the latest progress text (accumulated into `activities[]` by
 * ScenePage's onEvent) plus the pulsing `.think-dot` -- that per-attempt trail
 * is the only evidence the system is doing something for however long the
 * turn takes.
 *
 * D8 revises what happens once the turn ends: the trail was previously
 * re-rendered behind "See details", but it is mechanism noise once the work
 * is done (the interesting content -- the accountability record -- did not
 * even exist while the trail was live). So post-turn, the toggle instead
 * shows `message.details` (the resolve summary's numbered adjustments +
 * compound-turn time, set once by ScenePage when the turn_result artifact
 * lands) and `activities` is simply not read here anymore. The array itself
 * is left populated on the message (see conversationTypes.ts) -- there is no
 * separate "done, so clear it" write path; this render gate is the only
 * place the live/retained distinction is enforced, which is why it matters
 * that it stays here and not "helpfully" get relaxed later.
 *
 * See docs/phase4_spec_classifier_explain_activities.md section 6.3 and
 * docs/compound_turn_integration_spec.md §10b (D8).
 */
import { useState } from "react";
import type { ConversationMessage } from "./conversationTypes";
import { renderRefs } from "./renderRefs";
import type { SceneManifest } from "../authoring/types";

/** Ref-chip "kind", classified against the scene manifest (object vs facility). */
export type MessageRefKind = "object" | "facility";

function classifyRef(name: string, manifest: SceneManifest | null): MessageRefKind | null {
  if (!manifest) return null;
  if (Object.prototype.hasOwnProperty.call(manifest.objects, name)) return "object";
  if (Object.prototype.hasOwnProperty.call(manifest.facilities, name)) return "facility";
  return null;
}

/** Renders one assistant message's text: `[[ref:NAME]]` tags become chips
 *  (classified via the manifest), everything else renders as plain text with
 *  newlines preserved. Messages without tags render exactly as before. */
function RenderedMessageText({
  content,
  manifest,
  onRefClick,
}: {
  content: string;
  manifest: SceneManifest | null;
  onRefClick?: (name: string, kind: MessageRefKind, actionId?: string) => void;
}) {
  const segments = renderRefs(content);
  return (
    <span className="chat-assistant-text">
      {segments.map((segment, i) => {
        if (segment.type === "text") return <span key={i}>{segment.value}</span>;
        const kind = classifyRef(segment.name, manifest);
        if (!kind) return <span key={i}>{segment.name}</span>;
        return (
          <span
            key={i}
            className={`ref-token message-ref-chip${kind === "facility" ? " is-facility" : ""}`}
            role={onRefClick ? "button" : undefined}
            tabIndex={onRefClick ? 0 : undefined}
            onClick={onRefClick ? () => onRefClick(segment.name, kind, segment.actionId) : undefined}
          >
            {segment.name}
          </span>
        );
      })}
    </span>
  );
}

export function AssistantMessage({
  message,
  manifest = null,
  onRefClick,
}: {
  message: ConversationMessage;
  manifest?: SceneManifest | null;
  onRefClick?: (name: string, kind: MessageRefKind, actionId?: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  // D8: the toggle's content post-turn is the resolve summary's detail text
  // (numbered adjustments + makespan), one line per `\n`-separated entry --
  // NOT `message.activities`, which is the live attempt trail and is not
  // retained (see the file docstring). `showDetails` also covers the
  // fast-path/no-adjustments turn, where `details` is empty/undefined: no
  // toggle renders at all rather than an empty one.
  const detailLines = (message.details ?? "").split("\n").filter((line) => line.length > 0);
  const showDetails = message.status !== "working" && detailLines.length > 0;

  return (
    <div
      className={`chat-assistant${message.status === "working" ? " is-working" : ""}${
        message.status === "error" ? " is-error" : ""
      }`}
    >
      {showDetails ? (
        <div className="chat-steps-wrap">
          <button
            type="button"
            className="chat-steps-toggle"
            onClick={() => setExpanded((v) => !v)}
            aria-expanded={expanded}
          >
            {expanded ? "Hide details ▾" : "See details ▸"}
          </button>
          {expanded ? (
            <ul className="chat-steps">
              {detailLines.map((line, i) => (
                <li key={i}>
                  <RenderedMessageText content={line} manifest={manifest} onRefClick={onRefClick} />
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      <div className="chat-assistant-row">
        {message.status === "working" ? <span className="think-dot" aria-hidden="true" /> : null}
        <RenderedMessageText content={message.content} manifest={manifest} onRefClick={onRefClick} />
      </div>
    </div>
  );
}
