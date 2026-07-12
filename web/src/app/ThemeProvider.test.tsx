import { act, fireEvent, render, screen } from '@testing-library/react';

import { ThemeProvider, ThemeSelector } from './ThemeProvider';
import { THEME_PREFERENCE_KEY, readThemePreference } from './themePreference';

function installColorScheme(initialDark: boolean) {
  let dark = initialDark;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const query = {
    get matches() {
      return dark;
    },
    media: '(prefers-color-scheme: dark)',
    onchange: null,
    addEventListener: (
      _name: string,
      listener: (event: MediaQueryListEvent) => void,
    ) => listeners.add(listener),
    removeEventListener: (
      _name: string,
      listener: (event: MediaQueryListEvent) => void,
    ) => listeners.delete(listener),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  } as MediaQueryList;
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => query),
  );
  return {
    change(nextDark: boolean) {
      dark = nextDark;
      for (const listener of listeners) {
        listener({ matches: nextDark } as MediaQueryListEvent);
      }
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-theme-preference');
});

afterEach(() => vi.unstubAllGlobals());

it('defaults to System and follows live OS changes without restarting', () => {
  const system = installColorScheme(false);
  render(
    <ThemeProvider>
      <ThemeSelector />
    </ThemeProvider>,
  );

  expect(document.documentElement).toHaveAttribute('data-theme', 'light');
  expect(screen.getByRole('combobox', { name: '界面主题' })).toHaveValue(
    'system',
  );

  fireEvent.change(screen.getByRole('combobox', { name: '界面主题' }), {
    target: { value: 'dark' },
  });
  expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  act(() => system.change(false));
  expect(document.documentElement).toHaveAttribute('data-theme', 'dark');

  fireEvent.change(screen.getByRole('combobox', { name: '界面主题' }), {
    target: { value: 'system' },
  });
  act(() => system.change(true));
  expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
});

it('persists only the closed v1.1 theme enum and restores a fixed override', () => {
  installColorScheme(false);
  localStorage.setItem(THEME_PREFERENCE_KEY, 'dark');
  const { unmount } = render(
    <ThemeProvider>
      <ThemeSelector />
    </ThemeProvider>,
  );
  expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  unmount();

  localStorage.setItem(
    THEME_PREFERENCE_KEY,
    '{"theme":"dark","token":"secret"}',
  );
  expect(readThemePreference()).toBe('system');
  expect(localStorage.getItem(THEME_PREFERENCE_KEY)).toBeNull();
});
