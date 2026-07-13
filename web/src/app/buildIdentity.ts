const stableVersionSource =
  '(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*)';
const prereleaseIdentifier = '(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)';
const exactDesktopVersion = new RegExp(
  `^${stableVersionSource}(?:-${prereleaseIdentifier}(?:\\.${prereleaseIdentifier})*)?$`,
  'u',
);
const exactStableDesktopVersion = new RegExp(`^${stableVersionSource}$`, 'u');

export function isDesktopVersion(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length <= 64 &&
    exactDesktopVersion.test(value)
  );
}

export function isStableDesktopVersion(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length <= 64 &&
    exactStableDesktopVersion.test(value)
  );
}

export const DESKTOP_BUILD_VERSION: string | null = isDesktopVersion(
  __STOCK_DESK_DESKTOP_VERSION__,
)
  ? __STOCK_DESK_DESKTOP_VERSION__
  : null;

export function displayDesktopVersion(version: string | null): string {
  return !isDesktopVersion(version) ? '版本不可用' : `v${version}`;
}
