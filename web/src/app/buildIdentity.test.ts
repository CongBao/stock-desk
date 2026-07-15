import { DESKTOP_BUILD_VERSION, displayDesktopVersion } from './buildIdentity';

it('uses the exact Cargo/Tauri-injected prerelease identity', () => {
  expect(DESKTOP_BUILD_VERSION).toBe('1.1.0-beta.3');
  expect(displayDesktopVersion(DESKTOP_BUILD_VERSION)).toBe('v1.1.0-beta.3');
});

it('does not disguise a missing or invalid identity as a stable version', () => {
  expect(displayDesktopVersion(null)).toBe('版本不可用');
  expect(displayDesktopVersion('unavailable')).toBe('版本不可用');
  expect(displayDesktopVersion('1.1.0+local')).toBe('版本不可用');
  expect(displayDesktopVersion('1.1.0-01')).toBe('版本不可用');
  expect(displayDesktopVersion('1.1.0-alpha..1')).toBe('版本不可用');
});
