export type UiTheme = "dark" | "light";

export function resolveUiTheme(value: unknown): UiTheme {
  return typeof value === "string" && value.trim().toLowerCase() === "light"
    ? "light"
    : "dark";
}

export const uiTheme = resolveUiTheme(import.meta.env.VITE_UI_THEME);

