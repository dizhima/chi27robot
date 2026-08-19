/**
 * Pure parser for assistant message text: splits `[[ref:NAME]]` tags out of a
 * plain-text message into an ordered list of segments, so the renderer can
 * turn refs into chips while leaving everything else (including newlines)
 * untouched. See docs referenced from AssistantMessage.tsx for the feature.
 */

export type MessageSegment =
  | { type: "text"; value: string }
  | {
      type: "ref";
      name: string;
      /** Id of the action this tag belongs to (`[[ref:NAME|ACTION_ID]]`), so a
       *  chip can anchor to ONE task bar even when the same facility name
       *  appears in several lines. Absent for plain `[[ref:NAME]]` tags
       *  (note lines, removed actions, older messages). */
      actionId?: string;
    };

const REF_TAG_RE = /\[\[ref:([^\]]*)\]\]/g;

/**
 * Split `content` into text/ref segments. An unclosed or malformed `[[ref:`
 * (no matching `]]`, or an empty NAME) is left as plain text — never emitted
 * as a partial/invalid ref segment.
 */
export function renderRefs(content: string): MessageSegment[] {
  const segments: MessageSegment[] = [];
  let lastIndex = 0;
  REF_TAG_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = REF_TAG_RE.exec(content)) !== null) {
    // Payload is `NAME` or `NAME|ACTION_ID`; only the first `|` splits.
    const payload = match[1];
    const pipe = payload.indexOf("|");
    const name = (pipe === -1 ? payload : payload.slice(0, pipe)).trim();
    const actionId = pipe === -1 ? undefined : payload.slice(pipe + 1).trim() || undefined;
    if (!name) {
      // Malformed (empty NAME) — treat the whole matched text as plain text
      // by simply not splitting here; continue scanning past it.
      continue;
    }
    if (match.index > lastIndex) {
      segments.push({ type: "text", value: content.slice(lastIndex, match.index) });
    }
    segments.push(actionId ? { type: "ref", name, actionId } : { type: "ref", name });
    lastIndex = REF_TAG_RE.lastIndex;
  }
  if (lastIndex < content.length) {
    segments.push({ type: "text", value: content.slice(lastIndex) });
  }
  return segments;
}
