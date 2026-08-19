import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { RightWorkspace } from "./RightWorkspace";

vi.mock("./grounding", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./grounding")>()),
  loadSceneManifest: vi.fn(() =>
    Promise.resolve({ objects: {}, facilities: {} }),
  ),
  requestGround: vi.fn(),
}));

it("renders the multi-turn grounding workspace instead of the Codex terminal", () => {
  render(
    <RightWorkspace
      selectedBody={{ bodyId: 103, name: "mug_3" }}
      sceneFile="kitchen_scene.xml"
    />,
  );

  expect(screen.getByRole("region", { name: "Task authoring" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Grounding conversation" })).toBeInTheDocument();
  expect(screen.queryByTestId("codex-terminal-slot")).not.toBeInTheDocument();
});

it("renders the same grounding input when nothing is selected", () => {
  render(<RightWorkspace selectedBody={null} sceneFile="kitchen_scene.xml" />);
  expect(screen.getByLabelText("Task message")).toBeInTheDocument();
});
