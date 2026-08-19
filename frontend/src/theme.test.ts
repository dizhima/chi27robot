import { describe, expect, it } from "vitest";
import { resolveUiTheme } from "./theme";

describe("resolveUiTheme", () => {
  it("selects the light theme case-insensitively", () => {
    expect(resolveUiTheme("light")).toBe("light");
    expect(resolveUiTheme(" LIGHT ")).toBe("light");
  });

  it("keeps dark as the safe default", () => {
    expect(resolveUiTheme("dark")).toBe("dark");
    expect(resolveUiTheme("unknown")).toBe("dark");
    expect(resolveUiTheme(undefined)).toBe("dark");
  });
});

