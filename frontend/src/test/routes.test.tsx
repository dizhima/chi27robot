import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";

vi.mock("@mujoco/mujoco/mt", () => ({ default: vi.fn() }));
vi.mock("@mujoco/mujoco/mt/mujoco.wasm?url", () => ({ default: "/mock.wasm" }));
vi.mock("@react-three/drei", () => ({
  OrbitControls: () => null,
}));
vi.mock("mujoco-react", () => ({
  MujocoCanvas: () => null,
  MujocoProvider: ({ children }: { children: React.ReactNode }) => children,
  TrajectoryPlayer: () => null,
  DragInteraction: () => null,
  useSelectionHighlight: () => undefined,
}));

vi.stubGlobal(
  "fetch",
  vi.fn(() =>
    Promise.resolve({
      ok: false,
      status: 503,
      json: async () => ({ ok: false }),
    }),
  ),
);

import App from "../App";

it("renders scene launcher on /", () => {
  render(
    <MemoryRouter initialEntries={["/"]}>
      <App />
    </MemoryRouter>,
  );
  expect(screen.getByRole("heading", { name: "Open Scene" })).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Debug UI" })).not.toBeInTheDocument();
});

it("renders debug launcher on /debug", () => {
  render(
    <MemoryRouter initialEntries={["/debug"]}>
      <App />
    </MemoryRouter>,
  );
  expect(screen.getByRole("heading", { name: "Open Scene (Debug)" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Scene UI" })).toHaveAttribute("href", "/");
});
