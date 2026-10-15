import { createContext, useContext } from "react";

export type Theme = "light" | "dark";
const KEY = "eios-theme";

/** The saved choice, else dark. Storage can throw (private windows), so it is optional. */
export function initialTheme(): Theme {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") return saved;
  } catch {
    /* no storage: follow the OS */
  }
  return "dark"; // the design is dark first; light is an explicit choice
}

/** Tailwind's dark variant is the `dark` class on <html>. */
export function applyTheme(theme: Theme, persist = true): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  if (!persist) return;
  try {
    localStorage.setItem(KEY, theme);
  } catch {
    /* ignore */
  }
}

export const ThemeContext = createContext<Theme>("light");
export const useTheme = (): Theme => useContext(ThemeContext);
