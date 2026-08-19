import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";

it("renders React content", () => {
  render(<div>scene ui ready</div>);
  expect(screen.getByText("scene ui ready")).toBeInTheDocument();
});
