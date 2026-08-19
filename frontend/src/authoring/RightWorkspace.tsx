import { useEffect, useReducer } from "react";
import { AuthoringPanel } from "./AuthoringPanel";
import { loadSceneManifest, requestGround } from "./grounding";
import { createMockAuthoringState } from "./mockData";
import { authoringReducer } from "./reducer";
import type { SceneRef } from "./types";

type RightWorkspaceProps = {
  selectedBody: SceneRef | null;
  sceneFile: string;
};

export function RightWorkspace({ selectedBody, sceneFile }: RightWorkspaceProps) {
  const [state, dispatch] = useReducer(
    authoringReducer,
    undefined,
    createMockAuthoringState,
  );

  useEffect(() => {
    let active = true;
    loadSceneManifest()
      .then((manifest) => {
        if (active) dispatch({ type: "manifest_loaded", manifest });
      })
      .catch((error) => {
        if (active) {
          dispatch({
            type: "manifest_failed",
            error: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      active = false;
    };
  }, [sceneFile]);

  useEffect(() => {
    const pending = state.pendingGround;
    if (!pending) return;
    let active = true;
    requestGround(pending)
      .then((result) => {
        if (active) {
          dispatch({
            type: "ground_succeeded",
            requestId: pending.requestId,
            ...result,
          });
        }
      })
      .catch((error) => {
        if (active) {
          dispatch({
            type: "ground_failed",
            requestId: pending.requestId,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      active = false;
    };
  }, [state.pendingGround]);

  return (
    <aside className="right-workspace right-workspace-grounding">
      <section className="authoring-workspace" aria-label="Task authoring">
        <header className="authoring-workspace-header">
          <div>
            <strong>Task authoring</strong>
            <span>Multi-turn semantic grounding</span>
          </div>
        </header>
        <div className="authoring-workspace-body">
          <AuthoringPanel state={state} dispatch={dispatch} selectedBody={selectedBody} />
        </div>
      </section>
    </aside>
  );
}
