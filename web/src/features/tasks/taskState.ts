import type { TaskView } from './taskApi';

export function updateTaskSnapshot(
  items: readonly TaskView[],
  replacement: TaskView,
): readonly TaskView[] {
  const index = items.findIndex((item) => item.id === replacement.id);
  if (index < 0) return [replacement, ...items].slice(0, 100);
  return items.map((item) => {
    if (item.id !== replacement.id) return item;
    const currentTime = Date.parse(item.updatedAt);
    const replacementTime = Date.parse(replacement.updatedAt);
    if (
      replacementTime < currentTime ||
      (replacementTime === currentTime &&
        ((item.status !== 'queued' && item.status !== 'running') ||
          (item.cancelRequested && !replacement.cancelRequested)))
    ) {
      return item;
    }
    return replacement;
  });
}

export function mergeTaskSnapshots(
  current: readonly TaskView[],
  incoming: readonly TaskView[],
): readonly TaskView[] {
  return incoming.map((replacement) => {
    const existing = current.find((item) => item.id === replacement.id);
    return existing === undefined
      ? replacement
      : updateTaskSnapshot([existing], replacement)[0];
  });
}
