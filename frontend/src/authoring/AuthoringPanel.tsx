import { useEffect, useState } from "react";
import type { Dispatch } from "react";
import { IntentReview } from "./IntentReview";
import { robots } from "./mockData";
import { SemanticTaskList } from "./SemanticTaskList";
import { StrategySelector } from "./StrategySelector";
import type { AuthoringAction, AuthoringState, SceneRef } from "./types";

type AuthoringPanelProps = {
  state: AuthoringState;
  dispatch: Dispatch<AuthoringAction>;
  selectedBody: SceneRef | null;
};

export function AuthoringPanel({ state, dispatch, selectedBody }: AuthoringPanelProps) {
  const [draftMessage, setDraftMessage] = useState("");
  useEffect(() => {
    if (!state.deletedTask) return;
    const timer = window.setTimeout(() => dispatch({ type: "expire_undo" }), 5000);
    return () => window.clearTimeout(timer);
  }, [dispatch, state.deletedTask]);

  useEffect(() => {
    if (state.editingTargetTaskId && selectedBody) {
      dispatch({ type: "stage_target", target: selectedBody });
    }
  }, [dispatch, selectedBody, state.editingTargetTaskId]);

  if (state.phase === "clarifying") {
    return (
      <div className="authoring-clarifying">
        <section className="authoring-chat" aria-label="Grounding conversation">
          <div className="authoring-section-heading">
            <div>
              <span className="authoring-eyebrow">Clarifying</span>
              <h2>Describe the task</h2>
            </div>
            <span className="authoring-status">
              {state.pendingGround ? "Grounding…" : "Ready"}
            </span>
          </div>
          <div className="authoring-chat-messages" aria-live="polite">
            {state.messages.length === 0 ? (
              <p className="authoring-chat-empty">
                Ask naturally, then refine the complete semantic task list over multiple turns.
              </p>
            ) : (
              state.messages.map((message, index) => (
                <article
                  className={`authoring-chat-message is-${message.role}`}
                  key={`${message.role}-${index}`}
                >
                  <strong>{message.role === "user" ? "You" : "Assistant"}</strong>
                  <p>{message.content}</p>
                </article>
              ))
            )}
          </div>
          {state.groundError ? (
            <p className="authoring-ground-error" role="alert">
              {state.groundError}
            </p>
          ) : null}
          {state.groundingReason ? (
            <p className="authoring-ground-reason" role="status">
              {state.groundingReason}
            </p>
          ) : null}
          <form
            className="authoring-chat-composer"
            onSubmit={(event) => {
              event.preventDefault();
              if (!draftMessage.trim() || state.pendingGround) return;
              dispatch({ type: "send_message", text: draftMessage });
              setDraftMessage("");
            }}
          >
            <textarea
              aria-label="Task message"
              placeholder="例如：把两个杯子放到水池里"
              value={draftMessage}
              rows={2}
              onChange={(event) => setDraftMessage(event.target.value)}
            />
            <button
              type="submit"
              className="authoring-primary"
              disabled={!draftMessage.trim() || state.pendingGround !== null}
            >
              Send
            </button>
          </form>
        </section>

        {state.editingTargetTaskId ? (
          <section className="authoring-target-editor" aria-label="Target selection">
            <div>
              <span className="authoring-eyebrow">Editing target</span>
              <h2>Select a target in the scene</h2>
              <p>
                {state.pendingTarget
                  ? `Candidate: ${state.pendingTarget.name}`
                  : "Click a MuJoCo body to stage it as the new target."}
              </p>
            </div>
            <div className="authoring-actions">
              <button
                type="button"
                aria-label="Cancel target edit"
                onClick={() => dispatch({ type: "cancel_target_edit" })}
              >
                Cancel
              </button>
              <button
                type="button"
                className="authoring-primary"
                aria-label="Apply target"
                disabled={!state.pendingTarget}
                onClick={() => dispatch({ type: "apply_target" })}
              >
                Apply
              </button>
            </div>
          </section>
        ) : null}

        <SemanticTaskList
          tasks={state.tasks}
          robots={robots}
          onReassign={(taskId, robotId) =>
            dispatch({ type: "reassign_task", taskId, robotId })
          }
          onEditTarget={(taskId) => dispatch({ type: "begin_target_edit", taskId })}
          onDelete={(taskId) => dispatch({ type: "delete_task", taskId })}
        />
        {state.deletedTask ? (
          <div className="authoring-undo" role="status">
            <span>Task deleted</span>
            <button type="button" onClick={() => dispatch({ type: "undo_delete" })}>
              Undo
            </button>
          </div>
        ) : null}
        <div className="authoring-actions">
          <button
            type="button"
            className="authoring-primary"
            disabled={
              state.tasks.length === 0 ||
              state.pendingGround !== null ||
              state.editingTargetTaskId !== null
            }
            onClick={() => dispatch({ type: "confirm_plan" })}
          >
            Confirm semantic plan
          </button>
        </div>
      </div>
    );
  }

  if (state.phase === "intent_review") {
    return (
      <IntentReview
        summary={state.refinedIntent}
        onChange={(value) => dispatch({ type: "update_intent", value })}
        onConfirm={() => dispatch({ type: "confirm_intent" })}
      />
    );
  }

  if (state.phase === "plan_confirmed") {
    return (
      <section className="authoring-phase-message authoring-confirmed">
        <span className="authoring-eyebrow">Plan confirmed</span>
        <h2>Semantic task plan confirmed</h2>
        <p>Execution planning is not implemented yet</p>
      </section>
    );
  }

  return (
    <div className="authoring-plan-review">
      <section className="authoring-intent-compact">
        <div>
          <span className="authoring-eyebrow">Intent confirmed</span>
          <p>{state.refinedIntent}</p>
        </div>
        <button type="button" onClick={() => dispatch({ type: "edit_intent" })}>
          Edit
        </button>
      </section>
      {state.editingTargetTaskId ? (
        <section className="authoring-target-editor" aria-label="Target selection">
          <div>
            <span className="authoring-eyebrow">Editing target</span>
            <h2>Select a target in the scene</h2>
            <p>
              {state.pendingTarget
                ? `Candidate: ${state.pendingTarget.name}`
                : "Click a MuJoCo body to stage it as the new target."}
            </p>
          </div>
          <div className="authoring-actions">
            <button
              type="button"
              aria-label="Cancel target edit"
              onClick={() => dispatch({ type: "cancel_target_edit" })}
            >
              Cancel
            </button>
            <button
              type="button"
              className="authoring-primary"
              aria-label="Apply target"
              disabled={!state.pendingTarget}
              onClick={() => dispatch({ type: "apply_target" })}
            >
              Apply
            </button>
          </div>
        </section>
      ) : null}
      <StrategySelector
        strategies={state.strategies}
        selectedStrategyId={state.selectedStrategyId}
        onSelect={(strategyId) => dispatch({ type: "select_strategy", strategyId })}
      />
      <SemanticTaskList
        tasks={state.tasks}
        robots={robots}
        onReassign={(taskId, robotId) =>
          dispatch({ type: "reassign_task", taskId, robotId })
        }
        onEditTarget={(taskId) => dispatch({ type: "begin_target_edit", taskId })}
        onDelete={(taskId) => dispatch({ type: "delete_task", taskId })}
      />
      {state.deletedTask ? (
        <div className="authoring-undo" role="status">
          <span>Task deleted</span>
          <button type="button" onClick={() => dispatch({ type: "undo_delete" })}>
            Undo
          </button>
        </div>
      ) : null}
      <div className="authoring-actions">
        <button
          type="button"
          className="authoring-primary"
          disabled={state.tasks.length === 0 || state.editingTargetTaskId !== null}
          onClick={() => dispatch({ type: "confirm_plan" })}
        >
          Confirm semantic plan
        </button>
      </div>
    </div>
  );
}
