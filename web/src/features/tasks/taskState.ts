import type { TaskView } from './taskApi';

const lifecycleRank = {
  queued: 0,
  running: 1,
  succeeded: 2,
  failed: 2,
  cancelled: 2,
} as const;

function isTerminal(task: TaskView) {
  return lifecycleRank[task.status] === 2;
}

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
      lifecycleRank[replacement.status] < lifecycleRank[item.status] ||
      replacement.progress < item.progress ||
      (item.cancelRequested && !replacement.cancelRequested) ||
      (isTerminal(item) && replacement.status !== item.status)
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
