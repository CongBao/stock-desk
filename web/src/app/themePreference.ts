import { createContext, useContext } from 'react';

export type ThemePreference = 'system' | 'light' | 'dark';
export type ResolvedTheme = Exclude<ThemePreference, 'system'>;

export const THEME_PREFERENCE_KEY = 'stock-desk.preferences.v1.1.theme';
export const DARK_THEME_QUERY = '(prefers-color-scheme: dark)';
export const ALLOWED_THEMES = new Set<ThemePreference>([
  'system',
  'light',
  'dark',
]);

export function readThemePreference(
  storage: Storage = localStorage,
): ThemePreference {
  try {
    const value = storage.getItem(THEME_PREFERENCE_KEY);
    if (value === null) return 'system';
    if (ALLOWED_THEMES.has(value as ThemePreference)) {
      return value as ThemePreference;
    }
    storage.removeItem(THEME_PREFERENCE_KEY);
  } catch {
    // A denied preference store must not prevent the desktop workspace opening.
  }
  return 'system';
}

export function persistThemePreference(
  preference: ThemePreference,
  storage: Storage = localStorage,
) {
  try {
    // Only this closed enum is persisted; arbitrary JSON, URLs and secrets have no path here.
    storage.setItem(THEME_PREFERENCE_KEY, preference);
  } catch {
    // The active session still honors the user's choice when persistence is unavailable.
  }
}

export type ThemeContextValue = {
  readonly preference: ThemePreference;
  readonly resolvedTheme: ResolvedTheme;
  readonly setPreference: (preference: ThemePreference) => void;
};

export const ThemeContext = createContext<ThemeContextValue | null>(null);

export function systemTheme(): ResolvedTheme {
  return typeof window.matchMedia === 'function' &&
    window.matchMedia(DARK_THEME_QUERY).matches
    ? 'dark'
    : 'light';
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  return (
    value ?? {
      preference: 'system',
      resolvedTheme: systemTheme(),
      setPreference: () => undefined,
    }
  );
}
