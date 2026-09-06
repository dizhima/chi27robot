import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
} from "react";
import { ganttColorsForRobot } from "../robotVisuals";

/**
 * Rich chat composer backed by a single contentEditable div. Text flows
 * normally; scene references are atomic, non-editable inline token spans that
 * carry a `data-ref-id`. This is the inline "@-reference" surface: a token is
 * inserted at the caret (or appended) when the user double-clicks the 3D scene.
 *
 * The DOM is the source of truth (React never re-renders its innerHTML — that
 * would fight the caret), so all mutation is imperative: the parent drives token
 * insertion through the ref handle, and the composer reports document-order
 * `Part`s on send plus the live set of present ref ids on every edit (so the
 * parent can prune refs a backspace removed).
 */
export type ComposerPart =
  | { type: "text"; value: string }
  | { type: "ref"; refId: string };

export type ComposerHandle = {
  insertToken: (
    refId: string,
    label: string,
    kind: "object" | "facility" | "robot" | "position" | "plan_task",
    robot?: string,
  ) => void;
  updatePlanTaskToken: (refId: string, label: string, robot: string) => void;
  focus: () => void;
  clear: () => void;
  submit: () => void;
};

type RefComposerProps = {
  disabled?: boolean;
  placeholder?: string;
  onSend: (parts: ComposerPart[]) => void;
  /** Fired on every edit with the ref ids currently present in the document. */
  onRefsPresent?: (ids: string[]) => void;
  /** A token was clicked (to focus/adjust its 3D marker). */
  onTokenClick?: (refId: string) => void;
};

function serialize(root: HTMLElement): ComposerPart[] {
  const parts: ComposerPart[] = [];
  const pushText = (s: string) => {
    if (!s) return;
    const last = parts[parts.length - 1];
    if (last && last.type === "text") last.value += s;
    else parts.push({ type: "text", value: s });
  };
  const walk = (node: Node) => {
    node.childNodes.forEach((child) => {
      if (child.nodeType === Node.TEXT_NODE) {
        pushText(child.textContent ?? "");
      } else if (child instanceof HTMLElement) {
        if (child.dataset.refId) parts.push({ type: "ref", refId: child.dataset.refId });
        else if (child.tagName === "BR") pushText("\n");
        else walk(child);
      }
    });
  };
  walk(root);
  return parts;
}

function presentRefIds(root: HTMLElement): string[] {
  return Array.from(root.querySelectorAll<HTMLElement>("[data-ref-id]"))
    .map((el) => el.dataset.refId!)
    .filter(Boolean);
}

function updatePlanTaskTokenElement(
  token: HTMLElement,
  label: string,
  robot: string,
) {
  const colors = ganttColorsForRobot(robot);
  token.dataset.robot = robot;
  token.style.setProperty("--robot-bar-background", colors.background);
  token.style.setProperty("--robot-bar-border", colors.border);
  token.textContent = `▭ ${label}`;
}

export const RefComposer = forwardRef<ComposerHandle, RefComposerProps>(function RefComposer(
  { disabled = false, placeholder = "Instruct…", onSend, onRefsPresent, onTokenClick },
  ref,
) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  // Last caret Range inside the editor, so a scene pick inserts where the user
  // left off (the 3D click blurs the editor).
  const savedRangeRef = useRef<Range | null>(null);

  const isEmpty = (root: HTMLElement) =>
    root.querySelectorAll("[data-ref-id]").length === 0 &&
    (root.textContent ?? "").trim() === "";

  const refresh = useCallback(() => {
    const editor = editorRef.current;
    if (!editor) return;
    editor.classList.toggle("is-empty", isEmpty(editor));
    onRefsPresent?.(presentRefIds(editor));
  }, [onRefsPresent]);

  const saveSelection = useCallback(() => {
    const editor = editorRef.current;
    if (!editor) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;
    const range = sel.getRangeAt(0);
    if (editor.contains(range.commonAncestorContainer)) {
      savedRangeRef.current = range.cloneRange();
    }
  }, []);

  const doSend = useCallback(() => {
    const editor = editorRef.current;
    if (!editor || disabled) return;
    const parts = serialize(editor);
    const hasContent = parts.some(
      (p) => p.type === "ref" || (p.type === "text" && p.value.trim() !== ""),
    );
    if (!hasContent) return;
    onSend(parts);
  }, [disabled, onSend]);

  useImperativeHandle(
    ref,
    () => ({
      insertToken: (refId, label, kind, robot) => {
        const editor = editorRef.current;
        if (!editor) return;
        const token = document.createElement("span");
        token.className = `ref-token is-${kind}`;
        token.contentEditable = "false";
        token.dataset.refId = refId;
        if (kind === "plan_task" && robot) {
          updatePlanTaskTokenElement(token, label, robot);
        } else {
          token.textContent = kind === "position"
            ? `📍 ${label}`
            : kind === "robot"
              ? `● ${label}`
            : kind === "facility"
              ? `◇ ${label}`
              : kind === "plan_task"
                ? `▭ ${label}`
              : label;
        }

        const sel = window.getSelection();
        let range = savedRangeRef.current;
        if (!range || !editor.contains(range.commonAncestorContainer)) {
          range = document.createRange();
          range.selectNodeContents(editor);
          range.collapse(false); // caret at end
        }
        range.insertNode(token);
        range.setStartAfter(token);
        range.collapse(true);
        // Trailing space so the caret sits clear of the atomic token.
        const space = document.createTextNode(" ");
        range.insertNode(space);
        range.setStartAfter(space);
        range.collapse(true);
        if (sel) {
          sel.removeAllRanges();
          sel.addRange(range);
        }
        savedRangeRef.current = range.cloneRange();
        refresh();
      },
      updatePlanTaskToken: (refId, label, robot) => {
        const editor = editorRef.current;
        if (!editor) return;
        const token = Array.from(
          editor.querySelectorAll<HTMLElement>("[data-ref-id]"),
        ).find((candidate) => candidate.dataset.refId === refId);
        if (token) updatePlanTaskTokenElement(token, label, robot);
      },
      focus: () => editorRef.current?.focus(),
      clear: () => {
        const editor = editorRef.current;
        if (!editor) return;
        editor.textContent = "";
        savedRangeRef.current = null;
        refresh();
      },
      submit: doSend,
    }),
    [refresh, doSend],
  );

  // Mount-only: set the initial placeholder state. onRefsPresent must NOT be
  // called on every render (its identity changes each parent render, so a
  // render-triggered effect here becomes an update loop) — it fires only from
  // real edits (onInput) and imperative insert/clear.
  useEffect(() => {
    const editor = editorRef.current;
    if (editor) editor.classList.toggle("is-empty", isEmpty(editor));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      ref={editorRef}
      className="ref-composer is-empty"
      role="textbox"
      aria-multiline="true"
      aria-label="Instruction"
      data-placeholder={placeholder}
      contentEditable={!disabled}
      suppressContentEditableWarning
      onInput={refresh}
      onKeyUp={saveSelection}
      onMouseUp={saveSelection}
      onBlur={saveSelection}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          doSend();
        }
      }}
      onClick={(event) => {
        const target = (event.target as HTMLElement).closest<HTMLElement>("[data-ref-id]");
        if (target?.dataset.refId) onTokenClick?.(target.dataset.refId);
      }}
    />
  );
});
