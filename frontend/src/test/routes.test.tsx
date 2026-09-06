import { fireEvent, render, screen } from "@testing-library/react";
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
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Show preset scene paths" }));
  const options = screen.getAllByRole("option");
  expect(options).toHaveLength(8);
  expect(
    options.map((option) => option.textContent),
  ).toEqual([
    "assets/robocasa/layout042_sorting.xml",
    "assets/robocasa/layout024_sorting.xml",
    "assets/robocasa/layout024_sorting_heter.xml",
    "assets/robocasa/layout012_preparing.xml",
    "assets/robocasa/layout034_preparing.xml",
    "assets/robocasa/layout038_preparing.xml",
    "assets/robocasa/layout038_preparing_heter.xml",
    "assets/robocasa/layout049_sorting.xml",
  ]);
  fireEvent.click(screen.getByRole("option", { name: "assets/robocasa/layout024_sorting.xml" }));
  expect(screen.getByLabelText("Scene path")).toHaveValue(
    "assets/robocasa/layout024_sorting.xml",
  );
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});

it("renders debug launcher on /debug", () => {
  render(
    <MemoryRouter initialEntries={["/debug"]}>
      <App />
    </MemoryRouter>,
  );
  expect(screen.getByRole("heading", { name: "Open Scene (Debug)" })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Scene UI" })).toHaveAttribute("href", "/");
  fireEvent.click(screen.getByRole("button", { name: "Show preset scene paths" }));
  expect(screen.getAllByRole("option")).toHaveLength(8);
  fireEvent.click(screen.getByRole("option", { name: "assets/robocasa/layout038_preparing_heter.xml" }));
  expect(screen.getByLabelText("Scene path (relative to public/)")).toHaveValue(
    "assets/robocasa/layout038_preparing_heter.xml",
  );
});
