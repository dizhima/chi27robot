# Semantic Task Plan UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a mock-data-driven right-side co-authoring UI for refined-intent confirmation and semantic task-plan review, stopping before backend integration and execution planning.

**Architecture:** Add an `authoring/` feature with domain types, a pure reducer, deterministic mock data, and focused React components. `App.tsx` retains MuJoCo selection ownership and passes scene references into a new stacked right workspace; the existing Codex CLI remains below it. A later plan will replace the mock source with artifact APIs without changing the UI contracts.

**Tech Stack:** React, TypeScript, Vite, Vitest, Testing Library, `mujoco-react`, xterm.js.

---

## Scope

Implement Clarifying → Intent review → Plan review → Plan confirmed, Strategy A/B, one stable
`object_goal` task per object, robot reassignment, explicit scene-based target replacement,
delete/Undo, plan confirmation, and a resizable authoring/CLI split.

Do not implement artifact files, SSE, backend endpoints, execution actions, timelines,
trajectories, search/perception tasks, or task dependencies.

## File Map

- `src/authoring/types.ts`: authoring domain types.
- `src/authoring/mockData.ts`: deterministic kitchen showcase data.
- `src/authoring/reducer.ts`: pure state transitions.
- `src/authoring/IntentReview.tsx`: refined intent gate.
- `src/authoring/StrategySelector.tsx`: alternatives and recommendation.
- `src/authoring/SemanticTaskList.tsx`: individual task rows.
- `src/authoring/AuthoringPanel.tsx`: phase-aware structured workspace.
- `src/authoring/RightWorkspace.tsx`: authoring panel, divider, and CLI.
- `src/authoring/*.test.ts(x)`: reducer and component tests.
- `src/App.tsx`: selection integration.
- `src/TerminalPanel.tsx`: receives scene context.
- `src/style.css`: right workspace and authoring styles.
- `package.json`, `vite.config.ts`, `src/test/setup.ts`: Vitest setup.

---

### Task 1: Add the Frontend Test Harness

**Files:**
- Modify: `mujoco_react/frontend/package.json`
- Modify: `mujoco_react/frontend/vite.config.ts`
- Create: `mujoco_react/frontend/src/test/setup.ts`
- Create: `mujoco_react/frontend/src/test/smoke.test.tsx`

- [ ] **Step 1: Install test dependencies**

Run from `mujoco_react/frontend`:

```powershell
npm install --save-dev vitest jsdom @testing-library/react @testing-library/user-event @testing-library/jest-dom
```

Expected: the five packages appear under `devDependencies` and the lockfile changes.

- [ ] **Step 2: Add scripts and Vitest configuration**

Add to `package.json`:

```json
"test": "vitest run",
"test:watch": "vitest"
```

Preserve existing Vite options and add:

```ts
/// <reference types="vitest/config" />
test: {
  environment: "jsdom",
  setupFiles: ["./src/test/setup.ts"],
  css: true,
}
```

Create `src/test/setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
```

- [ ] **Step 3: Write and run a smoke test**

```tsx
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

it("renders React content", () => {
  render(<div>authoring ready</div>);
  expect(screen.getByText("authoring ready")).toBeInTheDocument();
});
```

Run `npm test -- --run src/test/smoke.test.tsx`.

Expected: one passing test.

- [ ] **Step 4: Commit**

```powershell
git add -- mujoco_react/frontend/package.json mujoco_react/frontend/package-lock.json mujoco_react/frontend/vite.config.ts mujoco_react/frontend/src/test
git commit -m "test: add frontend component test harness"
```

---

### Task 2: Define the Authoring Domain and Reducer

**Files:**
- Create: `mujoco_react/frontend/src/authoring/types.ts`
- Create: `mujoco_react/frontend/src/authoring/mockData.ts`
- Create: `mujoco_react/frontend/src/authoring/reducer.ts`
- Test: `mujoco_react/frontend/src/authoring/reducer.test.ts`

- [ ] **Step 1: Write failing reducer tests**

Cover intent confirmation, strategy switching with stable task IDs, one-task reassignment,
delete/Undo, target staging, target application, and plan confirmation:

```ts
it("confirms intent before exposing four tasks", () => {
  const next = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
  expect(next.phase).toBe("plan_review");
  expect(next.tasks).toHaveLength(4);
});

it("deletes and restores the same stable task", () => {
  const state = authoringReducer(createMockAuthoringState(), { type: "confirm_intent" });
  const deleted = authoringReducer(state, { type: "delete_task", taskId: "task-mug-1" });
  expect(deleted.tasks.some((task) => task.id === "task-mug-1")).toBe(false);
  const restored = authoringReducer(deleted, { type: "undo_delete" });
  expect(restored.tasks.some((task) => task.id === "task-mug-1")).toBe(true);
});
```

Run `npm test -- --run src/authoring/reducer.test.ts`.

Expected: FAIL because the feature modules do not exist.

- [ ] **Step 2: Define exact types**

Create `types.ts`:

```ts
export type AuthoringPhase = "clarifying" | "intent_review" | "plan_review" | "plan_confirmed";
export type SceneRef = { bodyId: number; name: string };
export type RobotId = "robot_a" | "robot_b";
export type StrategyId = "by_destination" | "by_region";
export type Strategy = { id: StrategyId; label: string; description: string; rationale: string; recommended: boolean };
export type ObjectGoalTask = {
  id: string;
  type: "object_goal";
  object: SceneRef;
  relation: "inside" | "on";
  target: SceneRef;
  assignee: RobotId;
};
export type AuthoringState = {
  phase: AuthoringPhase;
  refinedIntent: string;
  strategies: Strategy[];
  selectedStrategyId: StrategyId | null;
  tasks: ObjectGoalTask[];
  deletedTask: { task: ObjectGoalTask; index: number } | null;
  editingTargetTaskId: string | null;
  pendingTarget: SceneRef | null;
};
export type AuthoringAction =
  | { type: "confirm_intent" }
  | { type: "edit_intent" }
  | { type: "select_strategy"; strategyId: StrategyId }
  | { type: "reassign_task"; taskId: string; robotId: RobotId }
  | { type: "begin_target_edit"; taskId: string }
  | { type: "stage_target"; target: SceneRef }
  | { type: "apply_target" }
  | { type: "cancel_target_edit" }
  | { type: "delete_task"; taskId: string }
  | { type: "undo_delete" }
  | { type: "expire_undo" }
  | { type: "confirm_plan" };
```

- [ ] **Step 3: Add deterministic mock data**

Create Strategy A/B and four active tasks: mug 1 and mug 2 inside sink assigned to Robot A;
fruit A and fruit B inside fridge assigned to Robot B. Mug 3 appears only in the refined-intent
summary as excluded. Export `createMockAuthoringState()` with phase `intent_review`, no active
tasks, and no selected strategy.

- [ ] **Step 4: Implement the pure reducer**

Implement every action from `AuthoringAction`. `confirm_intent` copies the four mock tasks and
selects `by_destination`; `select_strategy` changes assignments but preserves task IDs, object,
relation, and target; `delete_task` stores the removed task and index; `undo_delete` restores it;
`apply_target` modifies only the selected task; every edit leaves the phase at `plan_review`.

Core target update:

```ts
case "apply_target":
  if (!state.editingTargetTaskId || !state.pendingTarget) return state;
  return {
    ...state,
    tasks: state.tasks.map((task) =>
      task.id === state.editingTargetTaskId
        ? { ...task, target: state.pendingTarget! }
        : task,
    ),
    editingTargetTaskId: null,
    pendingTarget: null,
  };
```

- [ ] **Step 5: Run tests and commit**

Run `npm test -- --run src/authoring/reducer.test.ts`.

Expected: all reducer tests pass.

```powershell
git add -- mujoco_react/frontend/src/authoring/types.ts mujoco_react/frontend/src/authoring/mockData.ts mujoco_react/frontend/src/authoring/reducer.ts mujoco_react/frontend/src/authoring/reducer.test.ts
git commit -m "feat: add semantic authoring state model"
```

---

### Task 3: Build Intent and Semantic Plan Components

**Files:**
- Create: `mujoco_react/frontend/src/authoring/IntentReview.tsx`
- Create: `mujoco_react/frontend/src/authoring/StrategySelector.tsx`
- Create: `mujoco_react/frontend/src/authoring/SemanticTaskList.tsx`
- Create: `mujoco_react/frontend/src/authoring/AuthoringPanel.tsx`
- Test: `mujoco_react/frontend/src/authoring/AuthoringPanel.test.tsx`

- [ ] **Step 1: Write failing component tests**

Test these behaviors with Testing Library:

```tsx
expect(screen.getByText(/leave mug 3 in place/i)).toBeInTheDocument();
expect(screen.queryByText("Strategy")).not.toBeInTheDocument();
await user.click(screen.getByRole("button", { name: "Confirm intent" }));
expect(dispatch).toHaveBeenCalledWith({ type: "confirm_intent" });
```

For Plan review, expect two strategy buttons, four elements with
`data-testid="semantic-task"`, individual robot selectors and Delete buttons, and no text matching
`navigate|pick|place|open refrigerator`.

Run `npm test -- --run src/authoring/AuthoringPanel.test.tsx`.

Expected: FAIL because the components do not exist.

- [ ] **Step 2: Implement IntentReview and StrategySelector**

`IntentReview` displays the summary and a `Confirm intent` button. `StrategySelector` renders one
button per strategy with recommendation, rationale, and `aria-pressed`; clicking calls
`onSelect(strategyId)`.

- [ ] **Step 3: Implement individual semantic task rows**

`SemanticTaskList` groups rows visually by assignee but keeps one row per object. Each row renders:

```text
[object name] [relation → target button] [robot select] [Delete]
```

Use `data-testid="semantic-task"`. Target buttons call `onEditTarget(task.id)`, robot selects call
`onReassign(task.id, robotId)`, and Delete calls `onDelete(task.id)`.

- [ ] **Step 4: Implement phase-aware AuthoringPanel**

Use props:

```ts
type AuthoringPanelProps = {
  state: AuthoringState;
  dispatch: React.Dispatch<AuthoringAction>;
  selectedBody: SceneRef | null;
};
```

Render only the current phase. In Plan review, collapse intent to one summary line with Edit,
then render Strategy and Tasks. When `deletedTask` exists, show Undo and dispatch `expire_undo`
after five seconds. In Plan confirmed, render exactly:

```text
Semantic task plan confirmed
Execution planning is not implemented yet
```

- [ ] **Step 5: Run tests and commit**

Run `npm test -- --run src/authoring/AuthoringPanel.test.tsx`.

Expected: all component tests pass.

```powershell
git add -- mujoco_react/frontend/src/authoring/IntentReview.tsx mujoco_react/frontend/src/authoring/StrategySelector.tsx mujoco_react/frontend/src/authoring/SemanticTaskList.tsx mujoco_react/frontend/src/authoring/AuthoringPanel.tsx mujoco_react/frontend/src/authoring/AuthoringPanel.test.tsx
git commit -m "feat: add semantic task plan review UI"
```

---

### Task 4: Connect Explicit MuJoCo Target Selection

**Files:**
- Modify: `mujoco_react/frontend/src/authoring/AuthoringPanel.tsx`
- Modify: `mujoco_react/frontend/src/App.tsx`
- Test: `mujoco_react/frontend/src/authoring/TargetSelection.test.tsx`

- [ ] **Step 1: Write failing target-mode tests**

Verify ordinary body selection does nothing when target mode is inactive. After
`begin_target_edit`, a changed `selectedBody` must dispatch `stage_target`; Apply dispatches
`apply_target`; Cancel dispatches `cancel_target_edit`.

Run `npm test -- --run src/authoring/TargetSelection.test.tsx`.

Expected: FAIL because target mode is not connected.

- [ ] **Step 2: Stage selections only in explicit target mode**

Add to `AuthoringPanel`:

```tsx
useEffect(() => {
  if (state.editingTargetTaskId && selectedBody) {
    dispatch({ type: "stage_target", target: selectedBody });
  }
}, [dispatch, selectedBody, state.editingTargetTaskId]);
```

Display “Select a target in the scene,” then show the pending XML-backed body name with Apply and
Cancel. Mock UI accepts any selected body; relation-aware validation belongs to the backend plan.

- [ ] **Step 3: Derive a stable scene reference in App**

```ts
const selectedBody = selectedBodyId !== null && selectedBodyName
  ? { bodyId: selectedBodyId, name: selectedBodyName }
  : null;
```

Preserve existing MuJoCo highlight and click-to-clear behavior.

- [ ] **Step 4: Run tests and commit**

Run:

```powershell
npm test -- --run src/authoring/TargetSelection.test.tsx src/authoring/reducer.test.ts
```

Expected: all tests pass.

```powershell
git add -- mujoco_react/frontend/src/App.tsx mujoco_react/frontend/src/authoring/AuthoringPanel.tsx mujoco_react/frontend/src/authoring/TargetSelection.test.tsx
git commit -m "feat: connect scene selection to task targets"
```

---

### Task 5: Integrate the Stacked Right Workspace

**Files:**
- Create: `mujoco_react/frontend/src/authoring/RightWorkspace.tsx`
- Modify: `mujoco_react/frontend/src/App.tsx`
- Modify: `mujoco_react/frontend/src/TerminalPanel.tsx`
- Modify: `mujoco_react/frontend/src/style.css`
- Test: `mujoco_react/frontend/src/authoring/RightWorkspace.test.tsx`

- [ ] **Step 1: Write a failing integration test**

Mock `TerminalPanel` and verify that the workspace renders a region named `Task authoring`, the
CLI slot, and scene context containing scene file, body ID, and body name.

```tsx
expect(screen.getByRole("region", { name: "Task authoring" })).toBeInTheDocument();
expect(screen.getByTestId("codex-terminal-slot")).toBeInTheDocument();
```

Run `npm test -- --run src/authoring/RightWorkspace.test.tsx`.

Expected: FAIL because `RightWorkspace` does not exist.

- [ ] **Step 2: Implement RightWorkspace with local mock state**

Use `useReducer(authoringReducer, undefined, createMockAuthoringState)`. Render the upper
`AuthoringPanel`, an accessible separator, and lower `TerminalPanel`. Maintain an upper-height
percentage clamped to 30–75 and update it from pointer movement relative to workspace height.

Props:

```ts
type RightWorkspaceProps = {
  selectedBody: SceneRef | null;
  sceneFile: string;
};
```

- [ ] **Step 3: Pass delimited scene context to the CLI**

```ts
const sceneContext = selectedBody
  ? `[SCENE_SELECTION]\nscene=${sceneFile}\nbodyId=${selectedBody.bodyId}\nname=${selectedBody.name}\n[/SCENE_SELECTION]`
  : "";
```

Use the existing `TerminalPanel.sceneContext` prop. Do not parse terminal output.

- [ ] **Step 4: Replace the terminal-only App child**

Replace `<TerminalPanel />` with:

```tsx
<RightWorkspace
  selectedBody={selectedBody}
  sceneFile={sceneSession?.sceneFile || sceneConfig.sceneFile}
/>
```

- [ ] **Step 5: Add focused responsive CSS**

Keep the existing 33vw right column. Add `.right-workspace`, scroll containment for the upper
panel, an 8px separator, and terminal containment below. Preserve the current under-900px layout.
Do not restyle the existing terminal internals.

- [ ] **Step 6: Run tests, build, and commit**

Run:

```powershell
npm test
npm run build
```

Expected: every test passes and TypeScript/Vite build succeeds.

```powershell
git add -- mujoco_react/frontend/src/App.tsx mujoco_react/frontend/src/TerminalPanel.tsx mujoco_react/frontend/src/style.css mujoco_react/frontend/src/authoring/RightWorkspace.tsx mujoco_react/frontend/src/authoring/RightWorkspace.test.tsx
git commit -m "feat: integrate co-authoring right workspace"
```

---

### Task 6: Verify the Complete Mock Showcase

**Files:**
- Modify only files required by failures found in verification.

- [ ] **Step 1: Run automated verification**

```powershell
npm test
npm run build
```

Expected: all Vitest suites pass; TypeScript and Vite build without errors.

- [ ] **Step 2: Run and inspect the app**

Run `npm run dev` from `mujoco_react/frontend` and verify:

1. MuJoCo remains the dominant left canvas.
2. The upper-right panel shows refined intent; the lower-right panel retains Codex CLI.
3. Confirming intent reveals two strategies and four individual tasks.
4. Strategy switching preserves task IDs and goal relations.
5. Robot reassignment changes one task.
6. Target changes require explicit mode, scene selection, and Apply.
7. Delete removes one task and Undo restores it.
8. Plan confirmation stops before execution planning.
9. The divider and content work at 1280×720 and 1920×1080.

- [ ] **Step 3: Review the diff**

Run `git status --short` and `git diff --check` from the repository root.

Expected: no whitespace errors; only frontend mock-UI files, dependency metadata, and this plan
are part of the work.

- [ ] **Step 4: Commit verification fixes if any**

```powershell
git add -- mujoco_react/frontend
git commit -m "test: verify semantic plan UI prototype"
```

Skip this commit if verification required no changes.

---

### Task 7: Make the Mock Intent Directly Editable

**Files:**
- Modify: `mujoco_react/frontend/src/authoring/types.ts`
- Modify: `mujoco_react/frontend/src/authoring/reducer.ts`
- Modify: `mujoco_react/frontend/src/authoring/IntentReview.tsx`
- Modify: `mujoco_react/frontend/src/authoring/AuthoringPanel.tsx`
- Test: `mujoco_react/frontend/src/authoring/reducer.test.ts`
- Test: `mujoco_react/frontend/src/authoring/AuthoringPanel.test.tsx`

- [ ] **Step 1: Write failing tests**

```tsx
await userEvent.clear(screen.getByLabelText("Intent"));
expect(screen.getByRole("button", { name: "Confirm intent" })).toBeDisabled();
await userEvent.type(screen.getByLabelText("Intent"), "Keep mug 3 on the island.");
expect(dispatch).toHaveBeenCalledWith({
  type: "update_intent",
  value: "Keep mug 3 on the island.",
});
```

Add a reducer assertion that `update_intent` changes `refinedIntent`, and that `edit_intent`
returns to `intent_review` without resetting the edited value.

- [ ] **Step 2: Run tests to verify RED**

Run `npm test -- --run src/authoring/reducer.test.ts src/authoring/AuthoringPanel.test.tsx`.

Expected: FAIL because `update_intent` and the textarea do not exist.

- [ ] **Step 3: Implement the minimal controlled editor**

Add this action:

```ts
| { type: "update_intent"; value: string }
```

Reduce it with:

```ts
case "update_intent":
  return { ...state, refinedIntent: action.value };
```

Render a `<textarea aria-label="Intent">`, dispatch on every change, and disable confirmation
when `summary.trim()` is empty. Keep fixed mock plan generation unchanged.

- [ ] **Step 4: Run targeted and full verification**

Run:

```powershell
npm test -- --run src/authoring/reducer.test.ts src/authoring/AuthoringPanel.test.tsx
npm test
npm run build
```

Expected: all tests and the production build pass.
