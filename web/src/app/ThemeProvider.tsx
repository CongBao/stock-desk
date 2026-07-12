import { type PropsWithChildren, useEffect, useMemo, useState } from 'react';
import {
  ALLOWED_THEMES,
  DARK_THEME_QUERY,
  persistThemePreference,
  readThemePreference,
  systemTheme,
  ThemeContext,
  type ThemeContextValue,
  type ThemePreference,
  useTheme,
} from './themePreference';

export function ThemeProvider({ children }: PropsWithChildren) {
  const [preference, setPreferenceState] = useState<ThemePreference>(() =>
    readThemePreference(),
  );
  const [systemPreference, setSystemPreference] = useState(systemTheme);
  const resolvedTheme = preference === 'system' ? systemPreference : preference;

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia(DARK_THEME_QUERY);
    const update = (event: MediaQueryListEvent) =>
      setSystemPreference(event.matches ? 'dark' : 'light');
    setSystemPreference(query.matches ? 'dark' : 'light');
    query.addEventListener('change', update);
    return () => query.removeEventListener('change', update);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme;
    document.documentElement.dataset.themePreference = preference;
    document.documentElement.style.colorScheme = resolvedTheme;
  }, [preference, resolvedTheme]);

  const value = useMemo<ThemeContextValue>(
    () => ({
      preference,
      resolvedTheme,
      setPreference(next) {
        if (!ALLOWED_THEMES.has(next)) return;
        persistThemePreference(next);
        setPreferenceState(next);
      },
    }),
    [preference, resolvedTheme],
  );

  return (
    <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
  );
}

export function ThemeSelector() {
  const { preference, setPreference } = useTheme();
  return (
    <label className="theme-selector">
      <span aria-hidden="true">◐</span>
      <span className="theme-selector-label">主题</span>
      <select
        aria-label="界面主题"
        value={preference}
        onChange={(event) =>
          setPreference(event.target.value as ThemePreference)
        }
      >
        <option value="system">跟随系统</option>
        <option value="light">浅色</option>
        <option value="dark">深色</option>
      </select>
    </label>
  );
}
